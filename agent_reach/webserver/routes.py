# -*- coding: utf-8 -*-
"""HTTP routes for the hosted client application.

Every route under /api (except the unauthenticated bootstrap) verifies the
caller's Supabase session server-side before anything runs. Browser-side login
proves nothing here: this API is reachable from the internet and anyone can
call it directly with curl.

Long operations run as background jobs and are polled, so a slow transcription
does not hold a request open for minutes.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from typing import Dict, Optional

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from agent_reach import __version__
from agent_reach.webserver import hosted_ops as ops
from agent_reach.webserver.auth import AuthError, User
from agent_reach.webserver.rate_limit import RateLimited

STATIC_DIR = Path(__file__).resolve().parent / "static"

MAX_JOBS = 200
MAX_JOB_LINES = 4000
JOB_TTL_SECONDS = 30 * 60
MAX_BODY_BYTES = 64 * 1024

router = APIRouter()


# --------------------------------------------------------------------------- #
# jobs
# --------------------------------------------------------------------------- #

class Job:
    __slots__ = ("id", "user_id", "operation", "status", "lines", "error",
                 "started", "finished")

    def __init__(self, job_id: str, user_id: str, operation: str):
        self.id = job_id
        self.user_id = user_id
        self.operation = operation
        self.status = "running"
        self.lines: list = []
        self.error: Optional[str] = None
        self.started = time.time()
        self.finished: Optional[float] = None

    def append(self, line: str) -> None:
        if len(self.lines) < MAX_JOB_LINES:
            self.lines.append(line)
        elif len(self.lines) == MAX_JOB_LINES:
            self.lines.append("… output truncated")

    def view(self, since: int) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "error": self.error,
            "lines": self.lines[since:],
            "next": len(self.lines),
            "elapsed": round((self.finished or time.time()) - self.started, 1),
        }


JOBS: Dict[str, Job] = {}
JOB_ORDER: list = []


def sweep_jobs() -> None:
    """Drop finished jobs once old, and cap how many are retained."""
    cutoff = time.time() - JOB_TTL_SECONDS
    while JOB_ORDER:
        oldest = JOB_ORDER[0]
        job = JOBS.get(oldest)
        if job is None:
            JOB_ORDER.pop(0)
            continue
        if len(JOB_ORDER) > MAX_JOBS or (job.finished and job.finished < cutoff):
            JOB_ORDER.pop(0)
            JOBS.pop(oldest, None)
            continue
        break


async def run_job(job: Job, stream) -> None:
    """Drain an already-validated stream off the event loop into the job."""
    def pump() -> None:
        try:
            for line in stream:
                job.append(line)
            job.status = "done"
        except ops.HostedError as exc:
            job.status, job.error = "error", str(exc)
        except Exception:                        # noqa: BLE001 — never leak internals
            job.status = "error"
            job.error = "Something went wrong running that. Please try again."
        finally:
            job.finished = time.time()

    await asyncio.to_thread(pump)


# --------------------------------------------------------------------------- #
# auth helper
# --------------------------------------------------------------------------- #

def _user(request: Request, authorization: Optional[str]) -> User:
    verifier = request.app.state.verifier
    if verifier is None:
        raise HTTPException(
            status_code=503,
            detail="This deployment is not configured for sign-in yet.",
        )
    try:
        return verifier.verify(authorization)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


# --------------------------------------------------------------------------- #
# public routes
# --------------------------------------------------------------------------- #

@router.get("/healthz", response_class=PlainTextResponse, include_in_schema=False)
async def healthz() -> str:
    """Liveness probe — no auth, no network calls, no database."""
    return "ok"


@router.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


@router.get("/api/config")
async def client_config(request: Request) -> JSONResponse:
    """Bootstrap values the sign-in page needs before a session exists.

    The anon key is designed to be public: it identifies the project and is
    useless without a valid session, because Row Level Security and Supabase's
    own auth enforce access. The JWT secret and service_role key are never
    served here.
    """
    settings = request.app.state.settings
    return JSONResponse({
        "supabaseUrl": settings.supabase_url,
        "supabaseAnonKey": settings.supabase_anon_key,
        "version": __version__,
        "operations": ops.available_operations(),
        "sources": {key: label for key, (label, _) in ops.SEARCH_PLATFORMS.items()},
        "transcription": ops.transcription_enabled(),
        "signInConfigured": bool(settings.supabase_url and settings.supabase_anon_key),
    })


# --------------------------------------------------------------------------- #
# authenticated routes
# --------------------------------------------------------------------------- #

@router.get("/api/me")
async def me(request: Request, authorization: Optional[str] = Header(None)) -> JSONResponse:
    user = _user(request, authorization)
    limiter = request.app.state.limiter
    return JSONResponse({
        "email": user.email,
        "limits": {
            name: limiter.remaining(user.id, name)
            for name in ops.available_operations()
        },
    })


@router.get("/api/status")
async def status(request: Request, authorization: Optional[str] = Header(None)) -> JSONResponse:
    """Real channel availability. Probes are live; labels are never invented."""
    _user(request, authorization)
    result = await asyncio.to_thread(ops.op_status, {})
    return JSONResponse(result)


@router.post("/api/run")
async def run(request: Request, authorization: Optional[str] = Header(None)) -> JSONResponse:
    user = _user(request, authorization)

    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="That request is too large.")
    try:
        import json as _json
        body = _json.loads(raw or b"{}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Malformed request.") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Malformed request.")

    name = body.get("operation")
    params = body.get("params") or {}
    if not isinstance(name, str) or not isinstance(params, dict):
        raise HTTPException(status_code=400, detail="Malformed request.")

    try:
        handler, streams = ops.get_operation(name)
    except ops.HostedError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not streams:
        try:
            request.app.state.limiter.check(user.id, name)
        except RateLimited as exc:
            return JSONResponse({"error": str(exc)}, status_code=429,
                                headers={"Retry-After": str(exc.retry_after)})
        try:
            return JSONResponse({"result": await asyncio.to_thread(handler, params)})
        except ops.HostedError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Validate before charging the allowance: the handler checks its parameters
    # eagerly and returns the stream, so a malformed request fails here with a
    # 400 instead of creating a job and spending a token.
    try:
        stream = await asyncio.to_thread(handler, params)
    except ops.HostedError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        request.app.state.limiter.check(user.id, name)
    except RateLimited as exc:
        return JSONResponse({"error": str(exc)}, status_code=429,
                            headers={"Retry-After": str(exc.retry_after)})

    sweep_jobs()
    job = Job(uuid.uuid4().hex, user.id, name)
    JOBS[job.id] = job
    JOB_ORDER.append(job.id)
    asyncio.create_task(run_job(job, stream))
    return JSONResponse({"job": job.id}, status_code=202)


@router.get("/api/jobs/{job_id}")
async def job_status(
    job_id: str, request: Request, since: int = 0,
    authorization: Optional[str] = Header(None),
) -> JSONResponse:
    user = _user(request, authorization)
    job = JOBS.get(job_id)
    # Identical answer whether the job is missing or belongs to someone else,
    # so a caller cannot probe for other people's job ids.
    if job is None or job.user_id != user.id:
        raise HTTPException(status_code=404, detail="That result is no longer available.")
    return JSONResponse(job.view(max(0, since)))
