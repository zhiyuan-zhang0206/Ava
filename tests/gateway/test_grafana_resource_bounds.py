"""Resource and local-hop boundaries for the Grafana gateway proxy."""

from __future__ import annotations

import asyncio
import http.server
import socketserver
import threading
from collections.abc import Iterator
from types import SimpleNamespace
from typing import cast

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.types import Scope
from starlette.websockets import WebSocketState

from gateway.routers import grafana
from shared import config
from shared.config.gateway import GatewaySettings


class _OkHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def local_http_server() -> Iterator[int]:
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _OkHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def test_local_grafana_hop_ignores_process_proxy_environment(
    local_http_server: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shell proxy cannot intercept or break the fixed loopback hop."""
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("NO_PROXY", "")

    async def scenario() -> None:
        async with grafana.build_proxy_client() as client:
            response = await client.get(f"http://127.0.0.1:{local_http_server}/")
        assert response.text == "ok"

    asyncio.run(scenario())


def test_upstream_cookie_never_becomes_shared_ambient_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_cookies: list[str | None] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen_cookies.append(request.headers.get("cookie"))
        headers = (
            {"Set-Cookie": "grafana_session=ambient"} if request.url.path.endswith("seed") else {}
        )
        return httpx.Response(200, headers=headers, content=b"ok")

    async def scenario() -> list[dict[str, object]]:
        client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        app_state = SimpleNamespace(grafana_client=client)

        async def call(path: str) -> list[dict[str, object]]:
            scope = cast(
                Scope,
                {
                    "type": "http",
                    "asgi": {"spec_version": "2.4"},
                    "http_version": "1.1",
                    "method": "GET",
                    "scheme": "http",
                    "path": f"/grafana/{path}",
                    "raw_path": f"/grafana/{path}".encode(),
                    "query_string": b"",
                    "headers": [],
                    "client": ("127.0.0.1", 1234),
                    "server": ("gateway.test", 8000),
                    "app": SimpleNamespace(state=app_state),
                },
            )

            async def receive() -> dict[str, object]:
                return {"type": "http.request", "body": b"", "more_body": False}

            messages: list[dict[str, object]] = []

            async def send(message: dict[str, object]) -> None:
                messages.append(message)

            response = await grafana._proxy(  # pyright: ignore[reportPrivateUsage]
                path,
                Request(scope, receive),  # pyright: ignore[reportArgumentType]
            )
            await response(scope, receive, send)  # pyright: ignore[reportArgumentType]
            return messages

        first = await call("seed")
        await asyncio.gather(*(call(f"read-{index}") for index in range(8)))
        await client.aclose()
        return first

    monkeypatch.setattr(config.settings.gateway, "grafana_proxy_enabled", True)
    monkeypatch.setattr(config.settings.gateway, "grafana_host", "127.0.0.1")
    monkeypatch.setattr(config.settings.gateway, "grafana_port", 3003)
    first_messages = asyncio.run(scenario())
    assert seen_cookies == [None] * 9
    raw_headers = cast(list[tuple[bytes, bytes]], first_messages[0]["headers"])
    response_headers = dict(raw_headers)
    assert b"set-cookie" not in response_headers


def test_slow_request_body_has_total_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    async def receive() -> dict[str, object]:
        await asyncio.sleep(0.05)
        return {"type": "http.request", "body": b"{}", "more_body": False}

    request = Request(
        cast(Scope, {"type": "http", "method": "POST", "path": "/", "headers": []}),
        receive,
    )
    monkeypatch.setattr(grafana, "_REQUEST_BODY_TIMEOUT_SECONDS", 0.01, raising=False)
    with pytest.raises(HTTPException) as raised:
        asyncio.run(grafana._bounded_request_body(request))  # pyright: ignore[reportPrivateUsage]
    assert raised.value.status_code == 408


def test_websocket_text_limit_counts_utf8_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Socket:
        def __init__(self) -> None:
            self.close_calls: list[tuple[int, str]] = []
            self.received = False

        async def receive(self) -> dict[str, str | int]:
            if not self.received:
                self.received = True
                return {"type": "websocket.receive", "text": "界" * 3}
            return {"type": "websocket.disconnect", "code": 1000}

        async def close(self, code: int, reason: str = "") -> None:
            self.close_calls.append((code, reason))

    class _Upstream:
        def __init__(self) -> None:
            self.close_calls: list[tuple[int, str]] = []
            self.sent: list[str | bytes] = []

        async def close(self, code: int, reason: str = "") -> None:
            self.close_calls.append((code, reason))

        async def send(self, payload: str | bytes) -> None:
            self.sent.append(payload)

    socket = _Socket()
    upstream = _Upstream()
    monkeypatch.setattr(grafana, "MAX_WS_MESSAGE_BYTES", 8)
    asyncio.run(
        grafana._client_to_grafana(  # pyright: ignore[reportArgumentType, reportPrivateUsage]
            socket,  # pyright: ignore[reportArgumentType]
            upstream,  # pyright: ignore[reportArgumentType]
        )
    )
    assert upstream.sent == []
    assert upstream.close_calls == [(1009, "message too large")]
    assert socket.close_calls == [(1009, "message too large")]


@pytest.mark.parametrize("host", ["localhost", "::1"])
def test_grafana_host_matches_ipv4_only_compose_listener(host: str) -> None:
    with pytest.raises(ValueError, match=r"127\.0\.0\.1"):
        GatewaySettings(AVA_GRAFANA_HOST=host)  # pyright: ignore[reportCallIssue]


@pytest.mark.parametrize("port", [0, -1, 65536])
def test_grafana_port_is_valid(port: int) -> None:
    with pytest.raises(ValueError):
        GatewaySettings(AVA_GRAFANA_PORT=port)  # pyright: ignore[reportCallIssue]


@pytest.mark.parametrize(
    "location",
    ["/grafana/../api/user", "/grafana/%2e%2e/api/user", "/grafana/%252e%252e/api/user"],
)
def test_redirect_cannot_escape_grafana_subpath(location: str) -> None:
    with pytest.raises(HTTPException) as raised:
        grafana._safe_redirect(  # pyright: ignore[reportPrivateUsage]
            location,
            "http://127.0.0.1:3003/grafana/",
            "http://127.0.0.1:3003",
        )
    assert raised.value.status_code == 502


@pytest.mark.parametrize(
    "query",
    [
        "password=",
        "API%5FKEY=secret",
        "session_token=x",
        "orgId=1&refresh_token=x",
        "orgId=",
        "orgId=1&orgId=2",
        "unknown",
    ],
)
def test_live_query_is_fail_closed_to_only_org_id(query: str) -> None:
    assert not grafana._websocket_query_allowed(query)  # pyright: ignore[reportPrivateUsage]


def test_live_query_accepts_only_required_org_id() -> None:
    assert grafana._websocket_query_allowed("")  # pyright: ignore[reportPrivateUsage]
    assert grafana._websocket_query_allowed("ORGID=1")  # pyright: ignore[reportPrivateUsage]


def test_live_does_not_forward_caller_subprotocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _Url:
        query = ""

    class _Socket:
        def __init__(self) -> None:
            self.headers = {
                "origin": "http://gateway.test:8000",
                "sec-websocket-protocol": "secret-token",
            }
            self.cookies: dict[str, str] = {}
            self.url = _Url()
            self.client_state = WebSocketState.CONNECTED
            self.application_state = WebSocketState.CONNECTED

        async def accept(self) -> None:
            return None

        async def close(self, code: int, reason: str) -> None:
            raise AssertionError(f"unexpected close {code}: {reason}")

    class _Upstream:
        pass

    class _Connect:
        async def __aenter__(self) -> _Upstream:
            return _Upstream()

        async def __aexit__(self, *_args: object) -> None:
            return None

    def fake_connect(*_args: object, **kwargs: object) -> _Connect:
        captured.update(kwargs)
        return _Connect()

    async def no_relay(*_args: object) -> None:
        return None

    monkeypatch.setattr(config.settings.gateway, "grafana_proxy_enabled", True)
    monkeypatch.setattr(config.settings.gateway, "grafana_host", "127.0.0.1")
    monkeypatch.setattr(config.settings.gateway, "gateway_url", "http://gateway.test:8000")
    monkeypatch.setattr(config.settings.gateway, "auth_middleware_enabled", False)
    monkeypatch.setattr(grafana, "connect", fake_connect)
    monkeypatch.setattr(grafana, "_relay_websocket", no_relay)
    asyncio.run(grafana.grafana_live(_Socket()))  # pyright: ignore[reportArgumentType]
    assert "subprotocols" not in captured


def test_websocket_close_is_not_sent_twice() -> None:
    class _Socket:
        client_state = WebSocketState.CONNECTED
        application_state = WebSocketState.DISCONNECTED

        async def close(self, code: int, reason: str) -> None:
            raise AssertionError(f"duplicate close {code}: {reason}")

    class _Upstream:
        close_code = 1000
        close_reason = "done"

        def __aiter__(self):  # pyright: ignore[reportMissingTypeArgument]
            async def empty():
                if False:
                    yield ""

            return empty()

    socket = _Socket()
    grafana._live_websockets.add(socket)  # pyright: ignore[reportArgumentType, reportPrivateUsage]
    asyncio.run(
        grafana._grafana_to_client(  # pyright: ignore[reportArgumentType, reportPrivateUsage]
            socket,  # pyright: ignore[reportArgumentType]
            _Upstream(),  # pyright: ignore[reportArgumentType]
        )
    )
    asyncio.run(grafana.close_live_websockets())
    assert not grafana._live_websockets  # pyright: ignore[reportPrivateUsage]
