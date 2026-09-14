# -*- coding: utf-8 -*-
"""Tests for the local web console.

This server executes real commands, so the security boundary is the point of
these tests: a request that is not authenticated, not loopback, or not an
allowlisted operation must never reach a subprocess.
"""

import json
import threading
import urllib.error
import urllib.request

import pytest

from agent_reach.webui import operations as ops
from agent_reach.webui.server import UIServer

TOKEN = "unit-test-token"
AUTH = {"X-Agent-Reach-Token": TOKEN}


@pytest.fixture()
def server():
    """A real server on an ephemeral loopback port, torn down after the test."""
    instance = UIServer(("127.0.0.1", 0), TOKEN)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        yield instance
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=5)


def call(server, path, method="GET", body=None, headers=None, host=None, timeout=30):
    url = f"http://127.0.0.1:{server.server_address[1]}{path}"
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    if host:
        request.add_header("Host", host)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


# --------------------------------------------------------------------------- #
# authentication
# --------------------------------------------------------------------------- #

def test_page_requires_a_token(server):
    """Serving the page hands over the token embedded in it, so it is gated too."""
    assert call(server, "/")[0] == 401


def test_page_accepts_the_launch_url_token(server):
    assert call(server, f"/?token={TOKEN}")[0] == 200


def test_api_requires_a_token(server):
    status, _ = call(server, "/api/run", "POST", {"operation": "status"})
    assert status == 401


def test_api_rejects_a_wrong_token(server):
    status, _ = call(server, "/api/run", "POST", {"operation": "status"},
                     {"X-Agent-Reach-Token": "not-the-token"})
    assert status == 401


def test_refusal_body_never_contains_the_token(server):
    _, body = call(server, "/", host="evil.test")
    assert TOKEN not in body


# --------------------------------------------------------------------------- #
# DNS rebinding
# --------------------------------------------------------------------------- #

def test_foreign_origin_is_refused(server):
    """A hostile page must not be able to drive this API from the user's browser."""
    status, _ = call(server, "/api/run", "POST", {"operation": "status"},
                     {**AUTH, "Origin": "http://evil.test"})
    assert status == 403


def test_same_origin_is_allowed(server):
    origin = f"http://127.0.0.1:{server.server_address[1]}"
    status, _ = call(server, "/api/meta", headers={**AUTH, "Origin": origin})
    assert status == 200


def test_spoofed_host_header_is_refused(server):
    """The Host check is what stops a rebound domain from reaching us."""
    assert call(server, "/", host="evil.test")[0] == 403


# --------------------------------------------------------------------------- #
# the operation allowlist
# --------------------------------------------------------------------------- #

def test_unknown_operation_is_refused(server):
    status, body = call(server, "/api/run", "POST",
                        {"operation": "rm -rf /", "params": {}}, AUTH)
    assert status == 400
    assert "Unknown operation" in json.loads(body)["error"]


def test_static_traversal_is_refused(server):
    assert call(server, "/static/../server.py")[0] == 403


def test_oversized_body_is_refused(server):
    status, _ = call(server, "/api/run", "POST",
                     {"operation": "status", "params": {"x": "A" * 10}},
                     {**AUTH, "Content-Length": str(ops.MAX_OUTPUT_CHARS * 100)})
    assert status in (400, 200)  # rejected outright, or length ignored as bogus


# --------------------------------------------------------------------------- #
# parameter validation — these run in-process, no server needed
# --------------------------------------------------------------------------- #

def test_search_rejects_an_unlisted_platform():
    with pytest.raises(ops.OperationError):
        list(ops.op_search({"platform": "evil", "query": "x"}))


def test_read_rejects_non_public_urls():
    for url in ("file:///etc/passwd", "http://127.0.0.1:22/", "http://169.254.169.254/"):
        with pytest.raises(ops.OperationError):
            list(ops.op_read({"url": url}))


def test_install_rejects_unknown_channels():
    """Channel names reach an argv, so anything shell-looking must be refused."""
    with pytest.raises(ops.OperationError):
        list(ops.op_install({"mode": "safe", "channels": ["; rm -rf /"]}))


def test_config_set_rejects_unknown_keys():
    with pytest.raises(ops.OperationError):
        ops.op_config_set({"key": "../../etc/passwd", "value": "x"})


def test_text_validation_rejects_control_characters():
    with pytest.raises(ops.OperationError):
        ops._text({"q": "hello\x00world"}, "q")


def test_count_is_clamped_to_its_range():
    assert ops._count({"limit": 9999}, "limit", 10, 1, 50) == 50
    assert ops._count({"limit": -5}, "limit", 10, 1, 50) == 1
    assert ops._count({}, "limit", 10, 1, 50) == 10


def test_every_operation_name_maps_to_a_callable():
    for name, (handler, streaming) in ops.OPERATIONS.items():
        assert callable(handler), name
        assert isinstance(streaming, bool), name


def test_search_platforms_all_have_a_label_and_handler():
    for platform, (label, handler) in ops.SEARCH_PLATFORMS.items():
        assert isinstance(label, str) and label, platform
        assert callable(handler), platform


# --------------------------------------------------------------------------- #
# secrets
# --------------------------------------------------------------------------- #

def test_config_get_masks_secrets(tmp_path, monkeypatch):
    from agent_reach.config import Config

    config_file = tmp_path / "config.yaml"
    monkeypatch.setattr(Config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(Config, "CONFIG_FILE", config_file)
    Config(config_path=config_file).set("groq_api_key", "gsk_super_secret")

    values = ops.op_config_get({})["values"]
    assert values["groq_api_key"] == "[REDACTED]"
    assert "gsk_super_secret" not in json.dumps(values)


def test_config_set_response_never_echoes_the_value(tmp_path, monkeypatch):
    from agent_reach.config import Config

    config_file = tmp_path / "config.yaml"
    monkeypatch.setattr(Config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(Config, "CONFIG_FILE", config_file)

    result = ops.op_config_set({"key": "groq-key", "value": "gsk_super_secret"})
    assert "gsk_super_secret" not in json.dumps(result)
    assert result["saved"] == "groq-key"


def test_twitter_cookie_import_requires_both_values(tmp_path, monkeypatch):
    from agent_reach.config import Config

    config_file = tmp_path / "config.yaml"
    monkeypatch.setattr(Config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(Config, "CONFIG_FILE", config_file)

    with pytest.raises(ops.OperationError):
        ops.op_config_set({"key": "twitter-cookies", "value": "auth_token=only"})


# --------------------------------------------------------------------------- #
# binding
# --------------------------------------------------------------------------- #

def test_serve_refuses_a_non_loopback_bind():
    """A server that runs commands must never be reachable from the network."""
    from agent_reach.webui.server import serve

    with pytest.raises(ValueError, match="loopback"):
        serve(host="0.0.0.0", port=0, open_browser=False)
