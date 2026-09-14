# -*- coding: utf-8 -*-
"""Loopback HTTP server backing the Agent Reach web UI.

This process executes real commands on the user's machine, so it is written to
be safe to leave running:

  * It binds 127.0.0.1 only — never 0.0.0.0, so nothing off the machine can
    reach it.
  * Every API request must carry the session token minted at startup. The token
    lives only in this process and in the launch URL.
  * The Host header must name a loopback address, and any Origin must match this
    exact server. Together these defeat DNS rebinding, where a hostile page
    resolves its own domain to 127.0.0.1 and then drives this API from the
    browser the user already trusts.
  * The browser never sends a command line. It names an operation from the
    allowlist in ``operations.py``, which builds the argv itself.

Standard library only: the project ships no web framework, and a single-user
loopback tool does not justify adding one.
"""

from __future__ import annotations

import json
import mimetypes
import os
import secrets
import threading
import time
import uuid
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Optional, cast
from urllib.parse import parse_qs, urlparse

from agent_reach import __version__
from agent_reach.webui import operations as ops

STATIC_DIR = Path(__file__).resolve().parent / "static"

#: Requests larger than this are refused before being read into memory.
MAX_REQUEST_BYTES = 2 * 1024 * 1024
#: Finished jobs are dropped once this many newer ones exist.
MAX_JOBS = 60
#: A job's captured lines are capped so a chatty tool cannot exhaust memory.
MAX_JOB_LINES = 5000

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}


class Job:
    """One background operation and the output it has produced so far."""

    __slots__ = ("id", "operation", "status", "lines", "error", "started", "finished", "lock")

    def __init__(self, job_id: str, operation: str):
        self.id = job_id
        self.operation = operation
        self.status = "running"          # running | done | error
        self.lines: list = []
        self.error: Optional[str] = None
        self.started = time.time()
        self.finished: Optional[float] = None
        self.lock = threading.Lock()

    def append(self, line: str) -> None:
        with self.lock:
            if len(self.lines) < MAX_JOB_LINES:
                self.lines.append(line)
            elif len(self.lines) == MAX_JOB_LINES:
                self.lines.append("… output truncated (line limit reached)")

    def snapshot(self, since: int) -> dict:
        with self.lock:
            return {
                "id": self.id,
                "operation": self.operation,
                "status": self.status,
                "error": self.error,
                "lines": self.lines[since:],
                "next": len(self.lines),
                "elapsed": round((self.finished or time.time()) - self.started, 1),
            }


class JobRegistry:
    def __init__(self) -> None:
        self._jobs: Dict[str, Job] = {}
        self._order: list = []
        self._lock = threading.Lock()

    def create(self, operation: str) -> Job:
        job = Job(uuid.uuid4().hex, operation)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            while len(self._order) > MAX_JOBS:
                stale = self._order.pop(0)
                self._jobs.pop(stale, None)
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)


JOBS = JobRegistry()


def _run_job(job: Job, handler, params: dict) -> None:
    try:
        for line in handler(params):
            job.append(line)
        job.status = "done"
    except ops.OperationError as exc:
        job.status = "error"
        job.error = str(exc)
    except Exception as exc:                      # noqa: BLE001 — never kill the server
        job.status = "error"
        job.error = f"Unexpected failure: {exc}"
    finally:
        job.finished = time.time()


class Handler(BaseHTTPRequestHandler):
    server_version = f"AgentReach/{__version__}"
    protocol_version = "HTTP/1.1"

    if TYPE_CHECKING:  # the base class types this as BaseServer
        server: "UIServer"

    @property
    def _bound_port(self) -> int:
        return cast("tuple[str, int]", self.server.server_address)[1]

    # -------------------------------------------------------------- helpers --
    def log_message(self, fmt, *args):            # noqa: A003 — quiet by default
        if os.environ.get("AGENT_REACH_UI_VERBOSE"):
            super().log_message(fmt, *args)

    def _send(self, status: int, payload: bytes, content_type: str,
              close: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, status: int, data: dict, close: bool = False) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", close=close)

    def _error(self, status: int, message: str) -> None:
        """Error responses always close the connection.

        A rejected POST is answered before its body has been read, and under
        HTTP/1.1 keep-alive that leaves unread bytes in the socket: the server
        would wait for a new request while the client is still writing the old
        body, and the connection dies with ConnectionAborted instead of
        delivering this response. Closing is the correct behaviour whenever a
        request is answered without consuming its body.
        """
        self._json(status, {"error": message}, close=True)

    # --------------------------------------------------------------- guards --
    def _host_is_loopback(self) -> bool:
        host = (self.headers.get("Host") or "").strip()
        if not host:
            return False
        name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
        return name in _LOOPBACK_HOSTS

    def _origin_ok(self) -> bool:
        """An absent Origin is fine (same-origin GET); a foreign one is not."""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        try:
            parsed = urlparse(origin)
        except ValueError:
            return False
        return (parsed.hostname or "") in {"127.0.0.1", "localhost", "::1"} and \
               parsed.port == self._bound_port

    def _authorized(self) -> bool:
        supplied = self.headers.get("X-Agent-Reach-Token", "")
        return secrets.compare_digest(supplied, self.server.token)

    def _query_authorized(self) -> bool:
        """Token from the launch URL, for the initial page load only.

        The page itself is gated too: serving the HTML means handing over the
        token embedded in it, so an unauthenticated GET of `/` would give the
        token away to anything that can reach the port.
        """
        query = parse_qs(urlparse(self.path).query)
        supplied = (query.get("token") or [""])[0]
        return secrets.compare_digest(supplied, self.server.token)

    def _preflight(self) -> bool:
        """Shared checks; writes the error response and returns False on failure."""
        if not self._host_is_loopback():
            self._error(HTTPStatus.FORBIDDEN, "This server only accepts loopback requests.")
            return False
        if not self._origin_ok():
            self._error(HTTPStatus.FORBIDDEN, "Cross-origin requests are refused.")
            return False
        return True

    # ------------------------------------------------------------------ GET --
    def do_GET(self):                             # noqa: N802 — stdlib contract
        if not self._preflight():
            return
        path = urlparse(self.path).path

        if path in ("/", "/index.html"):
            if not (self._query_authorized() or self._authorized()):
                return self._error(
                    HTTPStatus.UNAUTHORIZED,
                    "Missing or invalid session token. Use the link printed by "
                    "`agent-reach ui` — the token changes on every start.",
                )
            return self._serve_app()
        if path.startswith("/static/"):
            return self._serve_static(path[len("/static/"):])
        if path.startswith("/api/jobs/"):
            if not self._authorized():
                return self._error(HTTPStatus.UNAUTHORIZED, "Invalid session token.")
            return self._serve_job(path[len("/api/jobs/"):])
        if path == "/api/meta":
            if not self._authorized():
                return self._error(HTTPStatus.UNAUTHORIZED, "Invalid session token.")
            return self._json(HTTPStatus.OK, {
                "version": __version__,
                "platforms": {k: v[0] for k, v in ops.SEARCH_PLATFORMS.items()},
            })
        self._error(HTTPStatus.NOT_FOUND, "Not found")

    def _serve_app(self) -> None:
        """Serve the UI with this session's token injected.

        The token arrives in the launch URL's query string; the page reads it
        from the injected meta tag instead, so it is not re-sent on every
        navigation or leaked through a copied link in the address bar.
        """
        try:
            html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        except OSError:
            return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "UI files are missing.")
        html = html.replace("__AGENT_REACH_TOKEN__", self.server.token)
        self._send(HTTPStatus.OK, html.encode("utf-8"), "text/html; charset=utf-8")

    def _serve_static(self, relative: str) -> None:
        target = (STATIC_DIR / relative).resolve()
        try:
            target.relative_to(STATIC_DIR)
        except ValueError:
            return self._error(HTTPStatus.FORBIDDEN, "Refused")
        if not target.is_file():
            return self._error(HTTPStatus.NOT_FOUND, "Not found")
        kind = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self._send(HTTPStatus.OK, target.read_bytes(), kind)

    def _serve_job(self, remainder: str) -> None:
        job_id, _, since_raw = remainder.partition("/")
        job = JOBS.get(job_id)
        if job is None:
            return self._error(HTTPStatus.NOT_FOUND, "That job has expired.")
        try:
            since = int(since_raw or 0)
        except ValueError:
            since = 0
        self._json(HTTPStatus.OK, job.snapshot(max(0, since)))

    # ----------------------------------------------------------------- POST --
    def do_POST(self):                            # noqa: N802 — stdlib contract
        if not self._preflight():
            return
        if not self._authorized():
            return self._error(HTTPStatus.UNAUTHORIZED, "Invalid session token.")
        if urlparse(self.path).path != "/api/run":
            return self._error(HTTPStatus.NOT_FOUND, "Not found")

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._error(HTTPStatus.BAD_REQUEST, "Bad Content-Length")
        if length <= 0 or length > MAX_REQUEST_BYTES:
            return self._error(HTTPStatus.BAD_REQUEST, "Request body missing or too large")

        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._error(HTTPStatus.BAD_REQUEST, "Body must be UTF-8 JSON")
        if not isinstance(body, dict):
            return self._error(HTTPStatus.BAD_REQUEST, "Body must be a JSON object")

        name = body.get("operation")
        params = body.get("params") or {}
        if not isinstance(name, str) or not isinstance(params, dict):
            return self._error(HTTPStatus.BAD_REQUEST, "operation and params are required")

        try:
            handler, streaming = ops.get_operation(name)
        except ops.OperationError as exc:
            return self._error(HTTPStatus.BAD_REQUEST, str(exc))

        if streaming:
            job = JOBS.create(name)
            threading.Thread(
                target=_run_job, args=(job, handler, params), daemon=True,
            ).start()
            return self._json(HTTPStatus.ACCEPTED, {"job": job.id})

        try:
            return self._json(HTTPStatus.OK, {"result": handler(params)})
        except ops.OperationError as exc:
            return self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:                  # noqa: BLE001 — never kill the server
            return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"Unexpected failure: {exc}")


class UIServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False   # fail loudly rather than stealing a live port

    def __init__(self, address, token: str):
        super().__init__(address, Handler)
        self.token = token


def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    """Start the UI and block until interrupted."""
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError(
            "The web UI may only bind a loopback address — it runs commands on "
            "this machine and must never be reachable from the network."
        )

    token = secrets.token_urlsafe(32)
    try:
        server = UIServer((host, port), token)
    except OSError as exc:
        raise SystemExit(
            f"Could not start the web UI on {host}:{port} — {exc}\n"
            "Another process may already be using that port; try --port 8766."
        ) from exc

    actual_port = server.server_address[1]
    url = f"http://{host}:{actual_port}/?token={token}"

    print()
    print("  Agent Reach Console")
    print("  " + "─" * 52)
    print(f"  Open: {url}")
    print()
    print("  Loopback only — nothing outside this machine can reach it.")
    print("  The link contains a session token; it changes on every start.")
    print("  Press Ctrl+C to stop.")
    print()

    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
    finally:
        server.server_close()
