# -*- coding: utf-8 -*-
"""Direct MCP client for Exa's hosted search endpoint.

The CLI reaches Exa by shelling out to ``mcporter``, which means Node. A
container that only needs search should not carry a Node runtime for one HTTP
call, so this speaks Streamable HTTP MCP directly (verified against
mcp.exa.ai, protocol 2025-06-18): initialize, notifications/initialized, then
tools/call. Responses come back as SSE frames even for a single result, so the
reader below handles both plain JSON and ``data:`` frames.

No API key is required — the endpoint is free and anonymous.
"""

from __future__ import annotations

import json
import threading
from typing import Any, Optional

import requests

ENDPOINT = "https://mcp.exa.ai/mcp"
PROTOCOL_VERSION = "2025-06-18"
_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}
#: Exa can be slow on broad queries; well under the job timeout above it.
TIMEOUT = 45
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class ExaError(RuntimeError):
    """Exa could not be reached or returned something unusable."""


def _read_payload(response: requests.Response) -> dict:
    """Parse a JSON-RPC reply that may arrive as SSE frames."""
    raw = response.content[:MAX_RESPONSE_BYTES]
    text = raw.decode("utf-8", "replace")
    if "data:" in text:
        for line in text.splitlines():
            if line.startswith("data:"):
                try:
                    return json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
        raise ExaError("Exa returned an event stream with no readable payload")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ExaError("Exa returned a response that is not JSON") from exc


class ExaClient:
    """One MCP session, reused across requests.

    Sessions are cheap but not free, so the handshake is done once and reused;
    if the server forgets the session the next call re-initializes.
    """

    def __init__(self, endpoint: str = ENDPOINT, timeout: int = TIMEOUT):
        self.endpoint = endpoint
        self.timeout = timeout
        self._session = requests.Session()
        self._mcp_session_id: Optional[str] = None
        self._lock = threading.Lock()
        self._next_id = 0

    # ------------------------------------------------------------------ #

    def _post(self, body: dict, notify: bool = False) -> Optional[requests.Response]:
        headers = dict(_HEADERS)
        if self._mcp_session_id:
            headers["Mcp-Session-Id"] = self._mcp_session_id
        try:
            response = self._session.post(
                self.endpoint, headers=headers, json=body, timeout=self.timeout,
            )
        except requests.exceptions.SSLError as exc:
            raise ExaError(
                "TLS verification failed reaching Exa — see docs/troubleshooting.md"
            ) from exc
        except requests.RequestException as exc:
            raise ExaError(f"Could not reach Exa: {exc}") from exc
        if notify:
            return None
        if response.status_code >= 400:
            raise ExaError(f"Exa returned HTTP {response.status_code}")
        return response

    def _handshake(self) -> None:
        self._mcp_session_id = None
        response = self._post({
            "jsonrpc": "2.0", "id": self._advance(), "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "agent-reach", "version": "1.5.0"},
            },
        })
        assert response is not None
        self._mcp_session_id = response.headers.get("Mcp-Session-Id")
        _read_payload(response)  # surfaces a protocol error early
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, notify=True)

    def _advance(self) -> int:
        self._next_id += 1
        return self._next_id

    # ------------------------------------------------------------------ #

    def call_tool(self, name: str, arguments: dict) -> str:
        """Invoke one Exa tool and return its text content."""
        with self._lock:
            if not self._mcp_session_id:
                self._handshake()
            body = {
                "jsonrpc": "2.0", "id": self._advance(), "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
            response = self._post(body)
            assert response is not None
            payload = _read_payload(response)

            # A dropped session shows up as an error; redo the handshake once.
            if "error" in payload and "session" in json.dumps(payload).lower():
                self._handshake()
                body["id"] = self._advance()
                response = self._post(body)
                assert response is not None
                payload = _read_payload(response)

        if "error" in payload:
            message = (payload["error"] or {}).get("message", "unknown error")
            raise ExaError(f"Exa refused the request: {message}")

        content = ((payload.get("result") or {}).get("content")) or []
        parts = [
            item.get("text", "") for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        text = "\n".join(part for part in parts if part).strip()
        if not text:
            raise ExaError("Exa returned no results")
        return text

    # ------------------------------------------------------------------ #

    def search(self, query: str, num_results: int = 5) -> str:
        return self.call_tool(
            "web_search_exa", {"query": query, "numResults": num_results}
        )

    def fetch(self, url: str) -> str:
        return self.call_tool("web_fetch_exa", {"url": url})


#: Shared client — the handshake is reused across requests.
_client: Optional[ExaClient] = None
_client_lock = threading.Lock()


def get_client() -> ExaClient:
    global _client
    with _client_lock:
        if _client is None:
            _client = ExaClient()
        return _client


def search(query: str, num_results: int = 5) -> str:
    return get_client().search(query, num_results)


def fetch(url: str) -> Any:
    return get_client().fetch(url)
