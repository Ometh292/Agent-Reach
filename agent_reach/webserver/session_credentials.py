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
    #: The site the user signs in to.
    site: str
    #: Cookie domains accepted from a JSON export, so a paste taken from the
    #: wrong tab is rejected with a clear reason instead of silently failing.
    domains: tuple
    #: Numbered steps shown in the UI. Written for someone who has never
    #: installed a browser extension.
    steps: tuple
    #: True when the credential is passed to a child process as environment
    #: variables; False when its tool insists on a file, which this design
    #: cannot satisfy without writing to disk.
    env_only: bool = True


SUPPORTED: Dict[str, PlatformSpec] = {
    "twitter": PlatformSpec(
        id="twitter",
        label="Twitter / X",
        required=("auth_token", "ct0"),
        site="x.com",
        domains=(".x.com", "x.com", ".twitter.com", "twitter.com"),
        steps=(
            "Add the free Cookie-Editor extension to your browser.",
            "Open x.com in a new tab and make sure you are signed in.",
            "Click the Cookie-Editor icon in your browser toolbar. "
            "If you do not see it, click the puzzle-piece icon first.",
            "At the bottom of the panel click Export, then choose "
            "\u201cHeader String\u201d or \u201cJSON\u201d. Either works.",
            "Come back here and paste it into the box below.",
        ),
    ),
    "xueqiu": PlatformSpec(
        id="xueqiu",
        label="\u96ea\u7403 Xueqiu",
        required=("xq_a_token",),
        site="xueqiu.com",
        domains=(".xueqiu.com", "xueqiu.com"),
        steps=(
            "Add the free Cookie-Editor extension to your browser.",
            "Open xueqiu.com in a new tab and make sure you are signed in.",
            "Click the Cookie-Editor icon in your browser toolbar. "
            "If you do not see it, click the puzzle-piece icon first.",
            "At the bottom of the panel click Export, then choose "
            "\u201cHeader String\u201d or \u201cJSON\u201d. Either works.",
            "Come back here and paste it into the box below.",
        ),
    ),
}


#: Where to get the extension, per browser. Chrome's listing also serves Edge,
#: Brave and Opera, which all install from the Chrome Web Store.
EXTENSION_LINKS = {
    "Chrome, Edge, Brave or Opera":
        "https://chromewebstore.google.com/detail/cookie-editor/"
        "hlkenndednhfkekhgcdicdfddnkalmdm",
    "Firefox":
        "https://addons.mozilla.org/firefox/addon/cookie-editor/",
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


def _from_header_string(value: str) -> Dict[str, str]:
    """Parse `name=value; name=value` — Cookie-Editor's "Header String"."""
    found: Dict[str, str] = {}
    for part in value.replace("\n", ";").split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        name, _, cookie_value = part.partition("=")
        name, cookie_value = name.strip(), cookie_value.strip()
        if name and cookie_value:
            found[name] = cookie_value
    return found


def _from_json_export(value: str, domains: tuple) -> Optional[Dict[str, str]]:
    """Parse Cookie-Editor's JSON export, or return None if it is not JSON.

    Cookies from another site are dropped: a paste taken from the wrong tab
    should fail with a clear reason rather than appear to work.
    """
    import json

    stripped = value.lstrip()
    if not stripped.startswith("["):
        return None
    try:
        payload = json.loads(value)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, list):
        return None

    from agent_reach.utils.url import domain_matches

    allowed = tuple(d.lstrip(".") for d in domains)
    found: Dict[str, str] = {}
    wrong_domain = 0
    for item in payload:
        if not isinstance(item, dict):
            continue
        name, cookie_value = item.get("name"), item.get("value")
        if not isinstance(name, str) or not isinstance(cookie_value, str):
            continue
        domain = item.get("domain")
        if isinstance(domain, str) and domain and not domain_matches(domain, *allowed):
            wrong_domain += 1
            continue
        if name and cookie_value:
            found[name] = cookie_value
    # Signal "this was a real export, just from the wrong site" so the caller
    # can say that instead of "this does not look like a cookie export".
    if not found and wrong_domain:
        raise CredentialError(
            "That export came from a different website. Open the correct site "
            "in a tab, then export from there."
        )
    return found


def parse_cookie_export(value: str, required: tuple,
                        domains: tuple = ()) -> Dict[str, str]:
    """Extract the needed cookies from whichever export format was pasted.

    Cookie-Editor offers several export formats and a non-technical user has
    no way to know which one this wants, so accept both the header string and
    the JSON array. Only the named cookies are kept; a browser export contains
    a great deal else, and holding it would serve no purpose.
    """
    if not isinstance(value, str):
        raise CredentialError("Paste the exported cookie text.")
    value = value.strip()
    if not value:
        raise CredentialError("Paste the exported cookie text.")
    if len(value) > MAX_VALUE_CHARS:
        raise CredentialError(
            "That paste is longer than expected. Use Export \u2192 Header String, "
            "which is shorter than the full JSON."
        )

    parsed = _from_json_export(value, domains)
    if parsed is None:
        parsed = _from_header_string(value)

    found = {name: parsed[name] for name in required if name in parsed}
    missing = [name for name in required if name not in found]
    if missing:
        if not parsed:
            raise CredentialError(
                "That does not look like a cookie export. Use the Export button "
                "at the bottom of the Cookie-Editor panel."
            )
        raise CredentialError(
            "That export is missing " + " and ".join(missing)
            + ". It usually means you were not signed in, or the export came "
            "from a different tab. Open the site, check you are signed in, and "
            "export again."
        )
    return found


#: Retained under the old name so existing callers keep working.
parse_cookie_header = parse_cookie_export


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
        values = parse_cookie_export(pasted, spec.required, spec.domains)

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
        {
            "platform": spec.id,
            "label": spec.label,
            "site": spec.site,
            "steps": list(spec.steps),
            "required": list(spec.required),
            "extensions": EXTENSION_LINKS,
        }
        for spec in SUPPORTED.values()
    ]
