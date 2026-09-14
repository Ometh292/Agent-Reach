# -*- coding: utf-8 -*-
"""Hosted Agent Reach — FastAPI application factory.

Architecture, identical locally and in production:

    browser -> Supabase auth -> this API (JWT verified) -> Agent Reach -> internet

Unlike `agent_reach.webui` (the operator's local console, which installs
software and writes credentials and is bound to loopback), this application is
meant to face the internet, so:

  * every /api call is authenticated against Supabase server-side;
  * only read-only research operations exist — no install, no config writes,
    no shell;
  * every caller is rate limited, because each request spends the operator's
    quota rather than their own.

Run locally:
    uvicorn agent_reach.webserver.app:app --host 127.0.0.1 --port 8000
Or:
    python -m agent_reach.webserver.app
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from agent_reach import __version__
from agent_reach.webserver.auth import SupabaseVerifier
from agent_reach.webserver.rate_limit import RateLimiter, budgets_from_env
from agent_reach.webserver.routes import router
from agent_reach.webserver.session_credentials import SessionCredentials


def _load_dotenv_if_present() -> None:
    """Load a local .env when running outside a platform that injects env vars.

    Render and other hosts set real environment variables, where no .env file
    exists and this does nothing — so local and production read configuration
    through exactly the same path. Existing variables always win.

    Skipped under pytest. This mutates the process environment at import time,
    and a developer's local .env then leaks into unrelated tests: a real
    GROQ_API_KEY on disk made six transcription and installer tests believe a
    provider was configured when they required none.
    """
    if "pytest" in sys.modules or os.environ.get("PYTEST_CURRENT_TEST"):
        return
    try:
        from dotenv import load_dotenv
    except ImportError:                              # pragma: no cover
        return
    load_dotenv(override=False)


_load_dotenv_if_present()


@dataclass(frozen=True)
class Settings:
    supabase_url: str
    supabase_anon_key: str
    supabase_jwt_secret: Optional[str]
    allowed_origins: List[str]

    @classmethod
    def from_env(cls) -> "Settings":
        origins = [
            origin.strip()
            for origin in os.environ.get("ALLOWED_ORIGINS", "").split(",")
            if origin.strip()
        ]
        return cls(
            supabase_url=os.environ.get("SUPABASE_URL", "").rstrip("/"),
            supabase_anon_key=os.environ.get("SUPABASE_ANON_KEY", ""),
            supabase_jwt_secret=os.environ.get("SUPABASE_JWT_SECRET") or None,
            allowed_origins=origins,
        )


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or Settings.from_env()

    application = FastAPI(
        title="Agent Reach",
        version=__version__,
        docs_url=None,       # no interactive docs on a public deployment
        redoc_url=None,
        openapi_url=None,
    )

    application.state.settings = settings
    application.state.limiter = RateLimiter(budgets_from_env())
    # In-memory only. Never written to disk or a database; a restart
    # deliberately loses every connected session.
    application.state.credentials = SessionCredentials()

    # Built once at startup so a misconfiguration surfaces as a clear 503 from
    # /api/config rather than an import-time crash that takes the whole service
    # down — the sign-in page must still render to say what is missing.
    if settings.supabase_url and settings.supabase_anon_key:
        application.state.verifier = SupabaseVerifier(
            project_url=settings.supabase_url,
            anon_key=settings.supabase_anon_key,
            jwt_secret=settings.supabase_jwt_secret,
        )
    else:
        application.state.verifier = None

    # The frontend is served from this same origin, so CORS is only needed when
    # an operator deliberately hosts the UI elsewhere. Default: no cross-origin
    # access at all.
    if settings.allowed_origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=settings.allowed_origins,
            allow_credentials=False,       # bearer tokens, never cookies
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type"],
        )

    application.include_router(router)

    @application.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @application.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code,
                            headers=getattr(exc, "headers", None))

    @application.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        """Never return a stack trace or internal path to the internet."""
        return JSONResponse(
            {"error": "Something went wrong. Please try again."}, status_code=500,
        )

    return application


app = create_app()


def main() -> None:                              # pragma: no cover
    import uvicorn

    # 0.0.0.0 is required by container hosts, which route to the published
    # port; locally, pass --host 127.0.0.1 to keep it off the LAN.
    uvicorn.run(
        "agent_reach.webserver.app:app",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        proxy_headers=True,
        forwarded_allow_ips="*",
        log_level=os.environ.get("LOG_LEVEL", "info"),
    )


if __name__ == "__main__":                       # pragma: no cover
    main()
