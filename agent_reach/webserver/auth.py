# -*- coding: utf-8 -*-
"""Supabase session verification for the hosted deployment.

The API is reachable from the internet, so a browser-side login proves
nothing: anyone can call the endpoints directly. Every request therefore
carries the Supabase access token and the server verifies it before any
operation runs.

Two verification paths, in order of preference:

  1. Local signature check, when SUPABASE_JWT_SECRET is configured. No network
     round-trip. Supabase issues HS256 tokens signed with the project's JWT
     secret (Dashboard -> Settings -> API -> JWT Secret).
  2. Ask Supabase, otherwise. GET /auth/v1/user is always authoritative and
     needs no secret, at the cost of one request; results are cached briefly so
     a burst of calls does not become a burst of round-trips.

A token that fails both is rejected. There is deliberately no "trust the
client" fallback.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import requests

#: How long a successful remote verification is trusted before re-checking.
CACHE_SECONDS = 60
CACHE_LIMIT = 512
VERIFY_TIMEOUT = 10


class AuthError(Exception):
    """The caller is not authenticated. The message is safe to return."""


@dataclass(frozen=True)
class User:
    id: str
    email: str

    @property
    def short(self) -> str:
        """An identifier safe to put in logs."""
        return self.id[:8]


class SupabaseVerifier:
    def __init__(
        self,
        project_url: str,
        anon_key: str,
        jwt_secret: Optional[str] = None,
        audience: str = "authenticated",
    ):
        if not project_url:
            raise ValueError("SUPABASE_URL is required")
        if not anon_key:
            raise ValueError("SUPABASE_ANON_KEY is required")
        self.project_url = project_url.rstrip("/")
        self.anon_key = anon_key
        self.jwt_secret = jwt_secret or None
        self.audience = audience
        self._cache: Dict[str, Tuple[float, User]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #

    @staticmethod
    def _fingerprint(token: str) -> str:
        """Cache key that is not itself a usable credential."""
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _cached(self, key: str) -> Optional[User]:
        with self._lock:
            entry = self._cache.get(key)
            if not entry:
                return None
            expires, user = entry
            if expires < time.time():
                self._cache.pop(key, None)
                return None
            return user

    def _remember(self, key: str, user: User) -> None:
        with self._lock:
            if len(self._cache) >= CACHE_LIMIT:
                self._cache.clear()          # bounded and simple
            self._cache[key] = (time.time() + CACHE_SECONDS, user)

    # ------------------------------------------------------------------ #

    def _verify_locally(self, token: str) -> User:
        try:
            import jwt
        except ImportError as exc:                      # pragma: no cover
            raise AuthError("Server is missing the JWT library") from exc

        secret = self.jwt_secret
        if not secret:                                   # guarded by verify()
            raise AuthError("Your session could not be checked.")
        try:
            claims = jwt.decode(
                token,
                secret,
                algorithms=["HS256"],
                audience=self.audience,
                options={"require": ["exp", "sub"]},
            )
        except Exception as exc:
            # Never echo the library's message: it can describe the token.
            raise AuthError("Your session is not valid. Please sign in again.") from exc

        subject = claims.get("sub")
        if not subject:
            raise AuthError("Your session is missing a user id. Please sign in again.")
        return User(id=str(subject), email=str(claims.get("email") or ""))

    def _verify_remotely(self, token: str) -> User:
        try:
            response = requests.get(
                f"{self.project_url}/auth/v1/user",
                headers={"Authorization": f"Bearer {token}", "apikey": self.anon_key},
                timeout=VERIFY_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise AuthError("Could not reach the login service. Try again.") from exc

        if response.status_code == 401:
            raise AuthError("Your session has expired. Please sign in again.")
        if response.status_code >= 400:
            raise AuthError("The login service rejected this session.")

        try:
            payload = response.json()
        except ValueError as exc:
            raise AuthError("The login service returned an unreadable response.") from exc

        user_id = payload.get("id")
        if not user_id:
            raise AuthError("The login service returned no user id.")
        return User(id=str(user_id), email=str(payload.get("email") or ""))

    # ------------------------------------------------------------------ #

    def verify(self, authorization: Optional[str]) -> User:
        """Verify an ``Authorization: Bearer <token>`` header."""
        if not authorization:
            raise AuthError("Sign in to use this.")
        scheme, _, token = authorization.partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            raise AuthError("Sign in to use this.")
        if len(token) > 8192:
            raise AuthError("That session token is not valid.")

        key = self._fingerprint(token)
        cached = self._cached(key)
        if cached:
            return cached

        user = self._verify_locally(token) if self.jwt_secret else self._verify_remotely(token)
        self._remember(key, user)
        return user


def verifier_from_env() -> SupabaseVerifier:
    """Build a verifier from the deployment's environment variables."""
    return SupabaseVerifier(
        project_url=os.environ.get("SUPABASE_URL", ""),
        anon_key=os.environ.get("SUPABASE_ANON_KEY", ""),
        jwt_secret=os.environ.get("SUPABASE_JWT_SECRET"),
    )
