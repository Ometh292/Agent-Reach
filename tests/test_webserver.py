# -*- coding: utf-8 -*-
"""Tests for the hosted client application.

This API is meant to face the internet, so the security boundary is the
subject: an unauthenticated, expired, or forged session must never reach an
operation, and no browser-supplied value may become a command, a path, or an
internal address.

Authentication is exercised with real HS256 tokens signed by a test secret —
the same algorithm and verification path production uses — rather than by
patching the verifier out.
"""

import json
import time

import pytest

jwt = pytest.importorskip("jwt", reason="pyjwt is required for the hosted app")
pytest.importorskip("fastapi", reason="fastapi is required for the hosted app")
from fastapi.testclient import TestClient  # noqa: E402

from agent_reach.webserver import hosted_ops as ops  # noqa: E402
from agent_reach.webserver.app import Settings, create_app  # noqa: E402
from agent_reach.webserver.auth import AuthError, SupabaseVerifier  # noqa: E402
from agent_reach.webserver.rate_limit import (  # noqa: E402
    Budget,
    RateLimited,
    RateLimiter,
    budgets_from_env,
)

SECRET = "test-jwt-secret-not-a-real-one"
PROJECT = "https://example-project.supabase.co"


def make_token(sub="user-1", email="a@example.com", expires_in=3600, secret=SECRET,
               audience="authenticated"):
    now = int(time.time())
    return jwt.encode(
        {"sub": sub, "email": email, "aud": audience,
         "iat": now, "exp": now + expires_in},
        secret, algorithm="HS256",
    )


@pytest.fixture()
def client():
    app = create_app(Settings(
        supabase_url=PROJECT,
        supabase_anon_key="anon-key",
        supabase_jwt_secret=SECRET,
        allowed_origins=[],
    ))
    with TestClient(app) as test_client:
        yield test_client


def auth(token=None):
    return {"Authorization": f"Bearer {token or make_token()}"}


# --------------------------------------------------------------------------- #
# public surface
# --------------------------------------------------------------------------- #

def test_healthz_needs_no_auth(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.text == "ok"


def test_config_exposes_only_public_values(client):
    body = client.get("/api/config").json()
    assert body["supabaseAnonKey"] == "anon-key"
    assert body["signInConfigured"] is True
    # The secret must never be reachable from an unauthenticated endpoint.
    serialised = json.dumps(body)
    assert SECRET not in serialised
    assert "service_role" not in serialised
    assert "jwt_secret" not in serialised.lower()


def test_index_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Agent Reach" in response.text


def test_docs_are_disabled(client):
    """Interactive docs would advertise the API surface publicly."""
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


# --------------------------------------------------------------------------- #
# authentication
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("path", ["/api/me", "/api/status"])
def test_get_endpoints_require_auth(client, path):
    assert client.get(path).status_code == 401


def test_run_requires_auth(client):
    response = client.post("/api/run", json={"operation": "search",
                                             "params": {"platform": "exa", "query": "x"}})
    assert response.status_code == 401


def test_valid_token_is_accepted(client):
    response = client.get("/api/me", headers=auth())
    assert response.status_code == 200
    assert response.json()["email"] == "a@example.com"


def test_expired_token_is_rejected(client):
    response = client.get("/api/me", headers=auth(make_token(expires_in=-60)))
    assert response.status_code == 401


def test_token_signed_with_another_key_is_rejected(client):
    """A forged token must not pass — this is the whole point of verifying."""
    response = client.get("/api/me", headers=auth(make_token(secret="attacker-secret")))
    assert response.status_code == 401


def test_token_with_wrong_audience_is_rejected(client):
    response = client.get("/api/me", headers=auth(make_token(audience="anon")))
    assert response.status_code == 401


def test_garbage_token_is_rejected(client):
    for value in ("", "Bearer", "Bearer notatoken", "Basic abc", "Bearer " + "A" * 9000):
        response = client.get("/api/me", headers={"Authorization": value})
        assert response.status_code == 401, value


def test_auth_error_never_echoes_the_token(client):
    token = make_token(secret="attacker-secret")
    body = client.get("/api/me", headers=auth(token)).json()
    assert token not in json.dumps(body)


def test_verifier_rejects_missing_header():
    verifier = SupabaseVerifier(PROJECT, "anon-key", jwt_secret=SECRET)
    with pytest.raises(AuthError):
        verifier.verify(None)


# --------------------------------------------------------------------------- #
# operation allowlist / injection
# --------------------------------------------------------------------------- #

def test_unknown_operation_is_refused(client):
    response = client.post("/api/run", headers=auth(),
                           json={"operation": "install", "params": {}})
    assert response.status_code == 400


@pytest.mark.parametrize("forbidden", [
    "install", "config.set", "config.delete", "skill", "check_update", "tools",
])
def test_dangerous_console_operations_are_absent(client, forbidden):
    """The local console's privileged operations must not exist here at all."""
    assert forbidden not in ops.OPERATIONS
    response = client.post("/api/run", headers=auth(),
                           json={"operation": forbidden, "params": {}})
    assert response.status_code == 400


@pytest.mark.parametrize("payload", [
    "; rm -rf /", "$(whoami)", "`id`", "| cat /etc/passwd", "&& shutdown",
    "../../../../etc/passwd", "\x00truncated",
])
def test_shell_metacharacters_never_select_an_operation(client, payload):
    response = client.post("/api/run", headers=auth(),
                           json={"operation": payload, "params": {}})
    assert response.status_code == 400


def test_search_rejects_an_unlisted_source(client):
    response = client.post("/api/run", headers=auth(),
                           json={"operation": "search",
                                 "params": {"platform": "../../etc", "query": "x"}})
    assert response.status_code == 400


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "http://127.0.0.1:8000/api/config",
    "http://localhost/admin",
    "http://169.254.169.254/latest/meta-data/",   # cloud metadata
    "http://[::1]:8000/",
    "http://10.0.0.5/internal",
    "http://192.168.1.1/",
    "gopher://evil.test/",
])
def test_read_blocks_internal_and_non_http_targets(url):
    """SSRF boundary: a hosted server can otherwise be walked into its own network."""
    with pytest.raises(ops.HostedError):
        list(ops.op_read({"url": url}))


def test_youtube_rejects_non_youtube_urls():
    with pytest.raises(ops.HostedError):
        list(ops.op_youtube({"url": "https://example.com/video"}))


def test_control_characters_are_rejected():
    with pytest.raises(ops.HostedError):
        ops._text({"query": "hello\x00world"}, "query")


def test_oversized_body_is_refused(client):
    response = client.post("/api/run", headers=auth(),
                           content=b'{"operation":"search","params":{"query":"'
                                   + b"A" * 70000 + b'"}}')
    assert response.status_code == 413


def test_malformed_body_is_refused(client):
    assert client.post("/api/run", headers=auth(), content=b"not json").status_code == 400
    assert client.post("/api/run", headers=auth(), json=["a"]).status_code == 400


# --------------------------------------------------------------------------- #
# job isolation
# --------------------------------------------------------------------------- #

def test_jobs_are_not_readable_by_another_user(client):
    """Job ids must not be a way to read someone else's results."""
    started = client.post("/api/run", headers=auth(make_token(sub="user-A")),
                          json={"operation": "browse",
                                "params": {"feed": "v2ex_hot", "limit": 1}})
    assert started.status_code == 202
    job_id = started.json()["job"]

    response = client.get(f"/api/jobs/{job_id}",
                          headers=auth(make_token(sub="user-B", email="b@example.com")))
    assert response.status_code == 404


def test_unknown_job_is_404(client):
    response = client.get("/api/jobs/does-not-exist", headers=auth())
    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# rate limiting
# --------------------------------------------------------------------------- #

def test_limiter_allows_then_blocks():
    limiter = RateLimiter({"search": Budget(capacity=2, per_seconds=3600)})
    limiter.check("u", "search")
    limiter.check("u", "search")
    with pytest.raises(RateLimited) as excinfo:
        limiter.check("u", "search")
    assert excinfo.value.retry_after > 0


def test_limits_are_per_user():
    limiter = RateLimiter({"search": Budget(capacity=1, per_seconds=3600)})
    limiter.check("user-A", "search")
    limiter.check("user-B", "search")          # must not be affected by A


def test_limits_are_per_operation():
    limiter = RateLimiter({
        "search": Budget(capacity=1, per_seconds=3600),
        "read": Budget(capacity=1, per_seconds=3600),
    })
    limiter.check("u", "search")
    limiter.check("u", "read")


def test_transcription_budget_is_tighter_than_search():
    budgets = budgets_from_env()
    assert budgets["transcribe"].capacity < budgets["search"].capacity


def test_limits_are_configurable(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_SEARCH", "7/60")
    assert budgets_from_env()["search"] == Budget(7, 60)


def test_unparseable_limit_falls_back_to_default(monkeypatch):
    """A typo in one variable must not stop the service from booting."""
    monkeypatch.setenv("RATE_LIMIT_SEARCH", "not-a-budget")
    assert budgets_from_env()["search"].capacity > 0


def test_api_returns_429_when_exhausted(client):
    client.app.state.limiter = RateLimiter({"browse": Budget(capacity=1, per_seconds=3600)})
    first = client.post("/api/run", headers=auth(),
                        json={"operation": "browse", "params": {"feed": "v2ex_hot", "limit": 1}})
    assert first.status_code == 202
    second = client.post("/api/run", headers=auth(),
                         json={"operation": "browse", "params": {"feed": "v2ex_hot", "limit": 1}})
    assert second.status_code == 429
    assert "Retry-After" in second.headers


# --------------------------------------------------------------------------- #
# headers, CORS, secret leakage
# --------------------------------------------------------------------------- #

def test_security_headers_are_set(client):
    headers = client.get("/").headers
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Referrer-Policy"] == "no-referrer"


def test_api_responses_are_not_cached(client):
    assert client.get("/api/me", headers=auth()).headers["Cache-Control"] == "no-store"


def test_cors_is_closed_by_default(client):
    response = client.get("/api/config", headers={"Origin": "https://evil.test"})
    assert "access-control-allow-origin" not in {k.lower() for k in response.headers}


def test_cors_allows_only_configured_origins():
    app = create_app(Settings(PROJECT, "anon-key", SECRET, ["https://app.example.com"]))
    with TestClient(app) as configured:
        allowed = configured.get("/api/config", headers={"Origin": "https://app.example.com"})
        assert allowed.headers.get("access-control-allow-origin") == "https://app.example.com"
        denied = configured.get("/api/config", headers={"Origin": "https://evil.test"})
        assert denied.headers.get("access-control-allow-origin") != "https://evil.test"


def test_unconfigured_deployment_reports_503_not_a_crash():
    """A missing Supabase config must still render the sign-in page."""
    app = create_app(Settings("", "", None, []))
    with TestClient(app) as unconfigured:
        assert unconfigured.get("/").status_code == 200
        assert unconfigured.get("/api/config").json()["signInConfigured"] is False
        assert unconfigured.get("/api/me", headers=auth()).status_code == 503


# --------------------------------------------------------------------------- #
# status honesty
# --------------------------------------------------------------------------- #

def test_status_marks_only_truly_desktop_only_channels_unavailable():
    """Facebook and Instagram have no server-capable backend in this codebase.

    Reddit and 小红书 deliberately are NOT in this list: they have rdt-cli and
    xiaohongshu-mcp respectively, both of which run on a server from a saved
    cookie. Calling them impossible would be inaccurate — they are withheld by
    policy, not blocked by architecture.
    """
    channels = {c["id"]: c for c in ops.compute_status()}
    for channel_id in ("facebook", "instagram"):
        assert channels[channel_id]["state"] == "unavailable", channel_id
        assert channels[channel_id]["detail"], channel_id


def test_status_marks_credential_channels_as_needing_an_account():
    channels = {c["id"]: c for c in ops.compute_status()}
    for channel_id in ("twitter", "xueqiu", "linkedin", "reddit", "xiaohongshu"):
        assert channels[channel_id]["state"] == "needs_account", channel_id


def test_server_capable_channels_are_not_described_as_impossible():
    """Reddit and 小红书 have documented server backends; say so honestly."""
    channels = {c["id"]: c for c in ops.compute_status()}
    for channel_id in ("reddit", "xiaohongshu"):
        assert "Possible on a server" in channels[channel_id]["detail"], channel_id


def test_status_never_claims_a_channel_is_connected_without_evidence():
    allowed = {"available", "degraded", "disabled", "needs_account", "unavailable"}
    for channel in ops.compute_status():
        assert channel["state"] in allowed, channel


def test_transcription_hidden_without_a_key(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert ops.transcription_enabled() is False
    assert "transcribe" not in ops.available_operations()
    with pytest.raises(ops.HostedError):
        ops.get_operation("transcribe")


# --------------------------------------------------------------------------- #
# session-only credentials
#
# The promise is narrow and testable: a credential lives in memory, for one
# user, for a bounded time, and no endpoint ever reads it back out.
# --------------------------------------------------------------------------- #

from agent_reach.webserver.session_credentials import (  # noqa: E402
    IDLE_TTL_SECONDS,
    MAX_PER_USER,
    CredentialError,
    SessionCredentials,
    parse_cookie_header,
)

TW_COOKIE = "guest_id=v1%3A17; auth_token=abc123def; ct0=ff00ff; lang=en"


def test_parses_only_the_required_cookies():
    """A browser export contains a lot; keep only what the tool needs."""
    values = parse_cookie_header(TW_COOKIE, ("auth_token", "ct0"))
    assert values == {"auth_token": "abc123def", "ct0": "ff00ff"}
    assert "guest_id" not in values


def test_rejects_an_export_missing_a_required_cookie():
    with pytest.raises(CredentialError) as excinfo:
        parse_cookie_header("auth_token=only", ("auth_token", "ct0"))
    assert "ct0" in str(excinfo.value)


def test_rejects_an_oversized_paste():
    with pytest.raises(CredentialError):
        parse_cookie_header("a=" + "x" * 20000, ("a",))


def test_credentials_are_isolated_between_users():
    vault = SessionCredentials()
    vault.connect("user-A", "twitter", TW_COOKIE)
    assert vault.get("user-A", "twitter")["auth_token"] == "abc123def"
    assert vault.get("user-B", "twitter") is None


def test_disconnect_removes_the_credential():
    vault = SessionCredentials()
    vault.connect("u", "twitter", TW_COOKIE)
    assert vault.disconnect("u", "twitter") is True
    assert vault.get("u", "twitter") is None


def test_sign_out_clears_everything_for_that_user():
    vault = SessionCredentials()
    vault.connect("u", "twitter", TW_COOKIE)
    vault.connect("u", "xueqiu", "xq_a_token=tok")
    assert vault.disconnect_all("u") == 2
    assert vault.connected("u") == []


def test_credentials_expire_when_idle(monkeypatch):
    vault = SessionCredentials()
    vault.connect("u", "twitter", TW_COOKIE)

    real = __import__("time").time
    monkeypatch.setattr("agent_reach.webserver.session_credentials.time.time",
                        lambda: real() + IDLE_TTL_SECONDS + 10)
    assert vault.get("u", "twitter") is None


def test_connected_listing_never_contains_a_credential():
    """The listing tells the UI what is connected, never with what."""
    vault = SessionCredentials()
    vault.connect("u", "twitter", TW_COOKIE)
    blob = json.dumps(vault.connected("u"))
    assert "abc123def" not in blob
    assert "ff00ff" not in blob
    assert "twitter" in blob


def test_per_user_platform_count_is_bounded():
    vault = SessionCredentials()
    for index in range(MAX_PER_USER):
        vault._store.setdefault("u", {})[f"p{index}"] = vault.__class__ and __import__(
            "agent_reach.webserver.session_credentials", fromlist=["Entry"]
        ).Entry(values={"k": "v"})
    with pytest.raises(CredentialError):
        vault.connect("u", "twitter", TW_COOKIE)


def test_unknown_platform_cannot_be_connected():
    vault = SessionCredentials()
    with pytest.raises(CredentialError):
        vault.connect("u", "facebook", "a=b")


# ---- through the API ------------------------------------------------------ #

def test_connections_require_auth(client):
    assert client.get("/api/connections").status_code == 401
    assert client.post("/api/connections",
                       json={"platform": "twitter", "value": TW_COOKIE}).status_code == 401


def test_connect_then_list_then_disconnect(client):
    response = client.post("/api/connections", headers=auth(),
                           json={"platform": "twitter", "value": TW_COOKIE})
    assert response.status_code == 200
    assert [c["platform"] for c in response.json()["connected"]] == ["twitter"]

    listing = client.get("/api/connections", headers=auth()).json()
    assert [c["platform"] for c in listing["connected"]] == ["twitter"]
    # available catalogue must carry instructions but no secrets
    assert any(item["platform"] == "twitter" for item in listing["available"])

    removed = client.delete("/api/connections/twitter", headers=auth())
    assert removed.json()["connected"] == []


def test_api_never_returns_the_credential(client):
    client.post("/api/connections", headers=auth(),
                json={"platform": "twitter", "value": TW_COOKIE})
    for path in ("/api/connections", "/api/me", "/api/config"):
        body = client.get(path, headers=auth()).text
        assert "abc123def" not in body, path
        assert "ff00ff" not in body, path


def test_connecting_rejects_a_bad_paste_without_echoing_it(client):
    secret = "SUPERSECRETVALUE"
    response = client.post("/api/connections", headers=auth(),
                           json={"platform": "twitter", "value": f"wrong={secret}"})
    assert response.status_code == 400
    assert secret not in response.text


def test_search_on_a_connected_platform_asks_to_connect_first(client):
    """Well-formed but not connected: 409, naming the platform for the UI."""
    response = client.post("/api/run", headers=auth(),
                           json={"operation": "search",
                                 "params": {"platform": "twitter", "query": "x"}})
    assert response.status_code == 409
    assert response.headers.get("X-Connect-Platform") == "twitter"


def test_one_users_connection_does_not_serve_another(client):
    """The cross-user leak this design exists to prevent."""
    client.post("/api/connections", headers=auth(make_token(sub="user-A")),
                json={"platform": "twitter", "value": TW_COOKIE})
    response = client.post(
        "/api/run", headers=auth(make_token(sub="user-B", email="b@example.com")),
        json={"operation": "search", "params": {"platform": "twitter", "query": "x"}})
    assert response.status_code == 409


def test_clear_endpoint_drops_everything(client):
    client.post("/api/connections", headers=auth(),
                json={"platform": "twitter", "value": TW_COOKIE})
    assert client.post("/api/connections/clear", headers=auth()).json()["cleared"] == 1
    assert client.get("/api/connections", headers=auth()).json()["connected"] == []


def test_nothing_is_written_to_disk(tmp_path, monkeypatch):
    """The whole promise: no file appears anywhere as a result of connecting."""
    monkeypatch.chdir(tmp_path)
    before = set(tmp_path.rglob("*"))
    vault = SessionCredentials()
    vault.connect("u", "twitter", TW_COOKIE)
    vault.get("u", "twitter")
    assert set(tmp_path.rglob("*")) == before
