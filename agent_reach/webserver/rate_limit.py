# -*- coding: utf-8 -*-
"""Per-user rate limiting for the hosted deployment.

Every request spends the operator's quota, not the caller's: Exa searches,
GitHub API calls, and — if it is enabled — Whisper minutes that cost real
money. One signed-in user running a loop could drain a month of credit in an
afternoon, so limits are applied per user, not globally.

A token bucket is used rather than a fixed window: it allows a natural burst
(open four tabs, run four searches) while holding the long-run average down.

State lives in this process. On a single Render instance that is exactly
right; if the service is ever scaled to multiple instances, each gets its own
buckets and the effective limit multiplies by the instance count. At that
point this should move to Redis or Supabase — the deployment docs say so.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Dict, Tuple


class RateLimited(Exception):
    """Raised when a caller has spent their allowance."""

    def __init__(self, retry_after: int, message: str):
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True)
class Budget:
    """`capacity` requests available at once, refilling over `per_seconds`."""

    capacity: int
    per_seconds: int

    @property
    def refill_rate(self) -> float:
        return self.capacity / float(self.per_seconds)


#: Budgets by operation class. Cheap reads are generous; anything that spends
#: money or CPU is deliberately tight.
BUDGETS: Dict[str, Budget] = {
    "read":       Budget(capacity=60, per_seconds=3600),
    "search":     Budget(capacity=60, per_seconds=3600),
    "browse":     Budget(capacity=60, per_seconds=3600),
    "youtube":    Budget(capacity=30, per_seconds=3600),
    "transcribe": Budget(capacity=3,  per_seconds=3600),
}
DEFAULT_BUDGET = Budget(capacity=120, per_seconds=3600)


def budgets_from_env() -> Dict[str, Budget]:
    """Limits are configurable per deployment.

    RATE_LIMIT_SEARCH=60/3600 means sixty searches per hour. An unparseable
    value falls back to the built-in default rather than failing to boot —
    a typo in one variable should not take the service down.
    """
    import os

    budgets = dict(BUDGETS)
    for operation in list(BUDGETS) + ["youtube"]:
        raw = os.environ.get(f"RATE_LIMIT_{operation.upper()}")
        if not raw:
            continue
        try:
            capacity, _, window = raw.partition("/")
            parsed = Budget(int(capacity), int(window or 3600))
            if parsed.capacity > 0 and parsed.per_seconds > 0:
                budgets[operation] = parsed
        except (TypeError, ValueError):
            continue
    return budgets


class RateLimiter:
    def __init__(self, budgets: Dict[str, Budget] | None = None):
        self._budgets = dict(budgets or BUDGETS)
        self._buckets: Dict[Tuple[str, str], Tuple[float, float]] = {}
        self._lock = threading.Lock()

    def budget_for(self, operation: str) -> Budget:
        return self._budgets.get(operation, DEFAULT_BUDGET)

    def check(self, user_id: str, operation: str) -> None:
        """Spend one token, or raise RateLimited with a wait hint."""
        budget = self.budget_for(operation)
        key = (user_id, operation)
        now = time.monotonic()

        with self._lock:
            tokens, last = self._buckets.get(key, (float(budget.capacity), now))
            tokens = min(budget.capacity, tokens + (now - last) * budget.refill_rate)

            if tokens < 1.0:
                wait = int((1.0 - tokens) / budget.refill_rate) + 1
                self._buckets[key] = (tokens, now)
                raise RateLimited(
                    wait,
                    f"You have used this hour's allowance for {operation}. "
                    f"Try again in about {_friendly(wait)}.",
                )

            self._buckets[key] = (tokens - 1.0, now)

    def remaining(self, user_id: str, operation: str) -> int:
        """Whole requests still available, for the UI to show."""
        budget = self.budget_for(operation)
        with self._lock:
            tokens, last = self._buckets.get(
                (user_id, operation), (float(budget.capacity), time.monotonic())
            )
            tokens = min(
                budget.capacity, tokens + (time.monotonic() - last) * budget.refill_rate
            )
        return int(tokens)


def _friendly(seconds: int) -> str:
    if seconds < 90:
        return f"{seconds} seconds"
    minutes = round(seconds / 60)
    return f"{minutes} minute{'s' if minutes != 1 else ''}"
