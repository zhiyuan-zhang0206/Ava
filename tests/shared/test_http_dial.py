"""`shared/http_dial.py` — pin an IPv4-literal target to AF_INET, bypassing
`getaddrinfo` (and any DNS64/NAT64 synthesis for it) entirely.

These tests run against a real local TCP server (no network mocking of the
target itself) so `_PinnedIPv4Backend.connect_tcp` exercises a genuine
socket connect/read/write round trip; what's faked is `socket.getaddrinfo`
itself — patched to raise if called, which is the actual property under
test: the pinned path must never reach it for a literal host.
"""

from __future__ import annotations

import http.server
import socket
import threading
from collections.abc import Iterator

import httpx
import pytest

from shared.http_dial import get as dial_get
from shared.http_dial import post as dial_post
from shared.http_dial import put as dial_put
from shared.http_dial import transport_for_url


class _EchoHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass  # silence per-request stderr logging; signature matches the base class's

    def _respond(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def do_GET(self) -> None:
        self._respond()

    def do_POST(self) -> None:
        self._respond()

    def do_PUT(self) -> None:
        self._respond()


@pytest.fixture
def echo_server() -> Iterator[str]:
    """A real HTTP server on loopback (an IPv4 literal), yielding its base URL."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _EchoHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        thread.join()


@pytest.fixture
def forbid_getaddrinfo(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if anything under it calls socket.getaddrinfo — the
    property a pinned dial must guarantee for a literal target."""

    def _boom(*_a: object, **_kw: object) -> None:
        raise AssertionError("socket.getaddrinfo must not be called for an IPv4-literal dial")

    monkeypatch.setattr(socket, "getaddrinfo", _boom)


class TestTransportForUrl:
    def test_hostname_gets_no_transport(self) -> None:
        # None -> httpx's own default transport, unchanged behavior.
        assert transport_for_url("http://example.com/") is None

    def test_ipv4_literal_gets_pinned_transport(self) -> None:
        from shared.http_dial import PinnedIPv4Transport

        t = transport_for_url("http://100.64.0.72:8000/api/bootstrap")
        assert isinstance(t, PinnedIPv4Transport)

    def test_ipv6_literal_gets_no_transport(self) -> None:
        # Only the IPv4-literal-gets-v6-synthesized failure mode is in scope.
        assert transport_for_url("http://[::1]:8000/") is None


class TestPinnedDialNeverResolves:
    """The actual guarantee: get/post/put against an IPv4 literal never call
    getaddrinfo, and the round trip still works end to end."""

    def test_get(self, echo_server: str, forbid_getaddrinfo: None) -> None:
        resp = dial_get(f"{echo_server}/", timeout=3)
        assert resp.status_code == 200
        assert resp.content == b"ok"

    def test_post(self, echo_server: str, forbid_getaddrinfo: None) -> None:
        resp = dial_post(f"{echo_server}/", json={"a": 1}, timeout=3)
        assert resp.status_code == 200

    def test_put(self, echo_server: str, forbid_getaddrinfo: None) -> None:
        resp = dial_put(f"{echo_server}/", json={"a": 1}, timeout=3)
        assert resp.status_code == 200


class TestHostnameTargetsStayMonkeypatchable:
    """A non-literal URL must fall through to the real module-level
    httpx.get/post/put — the property existing tests across the repo rely on
    when they `monkeypatch.setattr("httpx.get", ...)` a gateway call site
    that now goes through shared.http_dial instead of httpx directly."""

    def test_get_delegates_to_httpx_get(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []

        def fake_get(url: str, **_kw: object) -> str:
            calls.append(url)
            return "stub-response"

        monkeypatch.setattr(httpx, "get", fake_get)
        assert dial_get("http://gw.example.com/api/health", timeout=3) == "stub-response"
        assert calls == ["http://gw.example.com/api/health"]

    def test_post_delegates_to_httpx_post(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []

        def fake_post(url: str, **_kw: object) -> str:
            calls.append(url)
            return "stub-response"

        monkeypatch.setattr(httpx, "post", fake_post)
        assert dial_post("http://gw.example.com/api/agents", timeout=3) == "stub-response"
        assert calls == ["http://gw.example.com/api/agents"]


class TestExceptionMapping:
    """Errors from the pinned path must surface as the same httpx exception
    types the stock backend raises — existing callers (shared/bootstrap.py,
    ava/_gateway_client.py) catch httpx.ConnectError / httpx.ConnectTimeout /
    httpx.TransportError specifically, not raw httpcore/socket errors."""

    def test_connection_refused_raises_httpx_connect_error(self) -> None:
        with pytest.raises(httpx.ConnectError):
            # Port 1 on loopback: nothing listens there, refused immediately.
            dial_get("http://127.0.0.1:1/", timeout=3)
