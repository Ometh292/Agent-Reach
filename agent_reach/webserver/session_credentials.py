# -*- coding: utf-8 -*-
"""Session-scoped platform credentials, held in memory and never persisted.

Some platforms only serve content to a signed-in session. Storing those logins
would make this service the custodian of its users' accounts: a database worth
attacking, and a breach that hands over real accounts rather than search
history.

This module takes the other option. A credential lives in this process, for one
user, for a bounded time, and is never written to disk, never written to a
database, never logged, and never returned by any endpoint. A restart loses
every credential, which is the intended behaviour rather than a limitation.

What this does NOT protect against, stated plainly:

  * The credential is in this process's memory while it is in use, so anyone who
    can read the process (a host operator, a memory dump, a debugger) can read
    it. Memory-only is a large reduction in risk, not elimination.
  * Python strings are immutable and cannot be reliably zeroed, so a deleted
    credential may persist in freed memory until the allocator reuses it.
  * The credential still travels from the browser to this server over the
    network, so TLS is doing real work — never run this over plain HTTP.

Users should be told all three. `docs/hosted-app.md` says so.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

#: A credential is forgotten this long after it was last used.
IDLE_TTL_SECONDS = 60 * 60
#: And unconditionally at this age, however active the session.
MAX_AGE_SECONDS = 8 * 60 * 60
#: Bounds so a flood of sessions cannot exhaust memory.
MAX_USERS = 500
MAX_PER_USER = 8
MAX_VALUE_CHARS = 8192


class CredentialError(Exception):
    """A user-facing problem with a supplied credential."""


@dataclass(frozen=True)
class PlatformSpec:
    """How one platform's credential is collected and handed to its tool."""

    id: str
    label: str
    #: Cookie names that must be present in the pasted export.
    required: tuple
    #: Plain-language instructions shown in the UI.
    how: str
    #: True when the credential is passed to a child process as environment
    #: variables; False when its tool insists on a file, which this design
    #: cannot satisfy without writing to disk.
    env_only: bool = True


SUPPORTED: Dict[str, PlatformSpec] = {
    "twitter": PlatformSpec(
        id="twitter",
        label="Twitter / X",
        required=("auth_token", "ct0"),
        how=(
            "Install the Cookie-Editor extension, open x.com while signed in, "
            "click the extension, choose Export → Header String, and paste it "
            "below."
        ),
    ),
    "xueqiu": PlatformSpec(
        id="xueqiu",
        label="雪球 Xueqiu",
        required=("xq_a_token",),
        how=(
            "Install the Cookie-Editor extension, open xueqiu.com while signed "
            "in, click the extension, choose Export → Header String, and paste "
            "it below."
        ),
    ),
}


@dataclass
class Entry:
    values: Dict[str, str]
    created: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)

    def expired(self, now: Optional[float] = None) -> bool:
        now = now or time.time()
        return (now - self.last_used > IDLE_TTL_SECONDS
                or now - self.created > MAX_AGE_SECONDS)


def parse_cookie_header(value: str, required: tuple) -> Dict[str, str]:
    """Extract the needed cookies from a Cookie-Editor "Header String" export.

    Only the named cookies are kept. Everything else in the paste — and a
    browser export contains a great deal else — is discarded immediately rather
    than held in memory for no reason.
    """
    if not isinstance(value, str):
        raise CredentialError("Paste the exported cookie text.")
    value = value.strip()
    if not value:
        raise CredentialError("Paste the exported cookie text.")
    if len(value) > MAX_VALUE_CHARS:
        raise CredentialError("That paste is too long to be a cookie export.")

    found: Dict[str, str] = {}
    for part in value.replace("\n", ";").split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        name, _, cookie_value = part.partition("=")
        name, cookie_value = name.strip(), cookie_value.strip()
        if name in required and cookie_value:
            found[name] = cookie_value

    missing = [name for name in required if name not in found]
    if missing:
        raise CredentialError(
            "That export does not contain " + " and ".join(missing)
            + ". Make sure you are signed in, and use Export → Header String."
        )
    return found


class SessionCredentials:
    """Per-user, in-memory credential store. Nothing here is ever persisted."""

    def __init__(self) -> None:
        self._store: Dict[str, Dict[str, Entry]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #

    def _purge_locked(self, now: float) -> None:
        for user_id in list(self._store):
            platforms = self._store[user_id]
            for platform in list(platforms):
                if platforms[platform].expired(now):
                    del platforms[platform]
            if not platforms:
                del self._store[user_id]

    def connect(self, user_id: str, platform: str, pasted: str) -> None:
        spec = SUPPORTED.get(platform)
        if spec is None:
            raise CredentialError("That platform cannot be connected here.")
        values = parse_cookie_header(pasted, spec.required)

        now = time.time()
        with self._lock:
            self._purge_locked(now)
            if user_id not in self._store and len(self._store) >= MAX_USERS:
                raise CredentialError(
                    "Too many active sessions right now. Please try again shortly."
                )
            platforms = self._store.setdefault(user_id, {})
            if platform not in platforms and len(platforms) >= MAX_PER_USER:
                raise CredentialError("Too many connected platforms.")
            platforms[platform] = Entry(values=values)

    def disconnect(self, user_id: str, platform: str) -> bool:
        with self._lock:
            platforms = self._store.get(user_id)
            if not platforms or platform not in platforms:
                return False
            del platforms[platform]
            if not platforms:
                self._store.pop(user_id, None)
            return True

    def disconnect_all(self, user_id: str) -> int:
        """Called on sign-out, so a credential does not outlive the session."""
        with self._lock:
            platforms = self._store.pop(user_id, None)
            return len(platforms or {})

    def get(self, user_id: str, platform: str) -> Optional[Dict[str, str]]:
        """Return the credential and mark it used, or None if absent/expired."""
        now = time.time()
        with self._lock:
            entry = (self._store.get(user_id) or {}).get(platform)
            if entry is None:
                return None
            if entry.expired(now):
                self.__delete_locked(user_id, platform)
                return None
            entry.last_used = now
            return dict(entry.values)

    def __delete_locked(self, user_id: str, platform: str) -> None:
        platforms = self._store.get(user_id)
        if platforms:
            platforms.pop(platform, None)
            if not platforms:
                self._store.pop(user_id, None)

    def connected(self, user_id: str) -> List[dict]:
        """Which platforms this user has connected — never any value.

        Deliberately returns no credential material at all: there is no code
        path in this application that reads a credential back out to a browser.
        """
        now = time.time()
        with self._lock:
            self._purge_locked(now)
            platforms = self._store.get(user_id) or {}
            return [
                {
                    "platform": platform,
                    "label": SUPPORTED[platform].label if platform in SUPPORTED else platform,
                    "expires_in": int(
                        min(
                            IDLE_TTL_SECONDS - (now - entry.last_used),
                            MAX_AGE_SECONDS - (now - entry.created),
                        )
                    ),
                }
                for platform, entry in sorted(platforms.items())
            ]

    def count(self) -> int:
        with self._lock:
            return sum(len(p) for p in self._store.values())


def catalogue() -> List[dict]:
    """What can be connected, for the UI. Contains no secrets."""
    return [
        {"platform": spec.id, "label": spec.label, "how": spec.how,
         "required": list(spec.required)}
        for spec in SUPPORTED.values()
    ]
