"""Gateway Grafana reverse-proxy tests.

`/grafana/*` forwards to a co-located Grafana instance (host/port from
AVA_GRAFANA_HOST / AVA_GRAFANA_PORT) behind the cluster auth middleware,
streaming the upstream response. Covers:
- explicit emergency disable: 404 on /grafana/*, nothing else changes
- enabled: GET root + nested path + query string forwarded
- trailing-slash canonicalization (/grafana -> /grafana/)
- POST with a JSON body + content-type forwarded (Grafana /api/ds/query)
- HEAD forwarded
- gzip upstream handled (identity requested; a gzip-ignoring upstream still
  yields a correctly decoded body without stale Content-Encoding/Length)
- 502: upstream unreachable
- auth required (middleware on: 401 without credentials)
- path-traversal rejection
"""

from __future__ import annotations

import asyncio
import gzip
import http.server
import json
import socket
import socketserver
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http import HTTPStatus
from types import SimpleNamespace
from typing import cast

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect, Request
from starlette.types import Scope
from starlette.websockets import WebSocketDisconnect, WebSocketState
from websockets.sync.server import ServerConnection, serve

from gateway.app import app
from gateway.routers import grafana
from shared import config
from shared.cluster_auth import cookie_name, sign_session

_SECRET = "test-cluster-secret"  # noqa: S105 — test fixture
_UPSTREAM_REQUESTS: list[dict[str, str]] = []


class _GrafanaHandler(http.server.BaseHTTPRequestHandler):
    """A minimal stand-in for Grafana: serves /, a nested JSON endpoint that
    echoes the POST body, and a path that ignores Accept-Encoding and always
    answers gzip-compressed."""

    def log_message(self, format: str, *args: object) -> None:
        pass

    def _send(
        self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        # The proxy preserves the /grafana prefix (serve_from_sub_path mode):
        # every upstream request arrives under it, root included.
        path = self.path[len("/grafana") :] if self.path.startswith("/grafana") else self.path
        _UPSTREAM_REQUESTS.append({k.lower(): v for k, v in self.headers.items()})
        if path.startswith("/api/search"):
            self._send(200, b'{"dashboards": []}', "application/json")
        elif path.startswith("/redirect-safe"):
            self._send(
                HTTPStatus.FOUND,
                b"",
                "text/plain",
                {
                    "Location": (
                        f"http://127.0.0.1:{cast(tuple[str, int], self.server.server_address)[1]}"
                        "/grafana/d/ava-ops-main"
                    )
                },
            )
        elif path.startswith("/redirect-evil"):
            self._send(
                HTTPStatus.FOUND,
                b"",
                "text/plain",
                {"Location": "https://evil.example/steal"},
            )
        elif path.startswith("/cookie"):
            self._send(
                200,
                b"ok",
                "text/plain",
                {"Set-Cookie": "grafana_session=must-not-escape; Path=/"},
            )
        elif path.startswith("/gzip"):
            body = gzip.compress(b"<h1>compressed</h1>")
            self._send(200, body, "text/html", {"Content-Encoding": "gzip"})
        elif path.startswith("/quiet-json"):
            body = b'{"eventually": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            time.sleep(0.05)
            self.wfile.write(body)
        elif path.startswith("/quiet-sse"):
            body = b"data: ready\n\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            time.sleep(0.05)
            self.wfile.write(body)
        elif path.startswith("/"):
            self._send(200, b"<h1>grafana</h1>", "text/html")

    def do_POST(self) -> None:
        assert self.path.startswith("/grafana/"), f"prefix must be preserved, got {self.path!r}"
        _UPSTREAM_REQUESTS.append({k.lower(): v for k, v in self.headers.items()})
        length = int(self.headers.get("Content-Length", "0"))
        payload = self.rfile.read(length)
        ct = self.headers.get("Content-Type", "")
        self._send(
            200, json.dumps({"echo": payload.decode(), "ct": ct}).encode(), "application/json"
        )

    def do_HEAD(self) -> None:
        self._send(200, b"", "text/html")


@pytest.fixture
def grafana_server() -> Iterator[int]:
    """A real HTTP server on 127.0.0.1 standing in for Grafana. Returns its port."""
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _GrafanaHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _UPSTREAM_REQUESTS.clear()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def _enable(monkeypatch: pytest.MonkeyPatch, port: int) -> None:
    monkeypatch.setattr(config.settings.gateway, "grafana_proxy_enabled", True)
    monkeypatch.setattr(config.settings.gateway, "grafana_host", "127.0.0.1")
    monkeypatch.setattr(config.settings.gateway, "grafana_port", port)
    monkeypatch.setattr(config.settings.gateway, "gateway_url", "http://testserver:8000")


def test_proxy_can_be_explicitly_disabled(
    grafana_server: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The emergency disable knob returns 404 without dialing Grafana."""
    monkeypatch.setattr(config.settings.gateway, "grafana_proxy_enabled", False)
    with TestClient(app) as client:
        resp = client.get("/grafana/")
    assert resp.status_code == 404
    assert "grafana proxy is disabled" in resp.text


def test_proxy_serves_content(grafana_server: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """Enabled: GET through the gateway returns Grafana's content — root,
    nested path, and query string all work."""
    _enable(monkeypatch, grafana_server)
    with TestClient(app) as client:
        root = client.get("/grafana/")
        assert root.status_code == 200
        assert root.headers["content-type"].startswith("text/html")
        assert "<h1>grafana</h1>" in root.text

        nested = client.get("/grafana/api/search?query=ops")
        assert nested.status_code == 200
        assert nested.headers["content-type"].startswith("application/json")
        assert nested.json() == {"dashboards": []}


def test_proxy_redirects_root_without_trailing_slash(
    grafana_server: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """/grafana (no slash) redirects to /grafana/ so the app's relative asset
    links resolve against the proxy root, not the gateway API root."""
    _enable(monkeypatch, grafana_server)
    with TestClient(app) as client:
        resp = client.get("/grafana", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/grafana/"


def test_proxy_forwards_post_body(grafana_server: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """POST (Grafana /api/ds/query) forwards the JSON body and Content-Type —
    the dashboard frontend queries datasources through this path."""
    _enable(monkeypatch, grafana_server)
    body = {"queries": [{"refId": "A"}]}
    with TestClient(app) as client:
        resp = client.post("/grafana/api/ds/query", json=body)
    assert resp.status_code == 200
    echo = resp.json()
    assert json.loads(echo["echo"]) == body
    assert echo["ct"].startswith("application/json")


def test_proxy_replaces_spoofable_identity_and_strips_browser_credentials(
    grafana_server: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the gateway-created fixed viewer identity reaches Grafana. Ava's
    cookie/bearer and any caller-supplied auth-proxy headers stop here."""
    _enable(monkeypatch, grafana_server)
    with TestClient(app) as client:
        resp = client.get(
            "/grafana/api/search",
            headers={
                "Authorization": "Bearer browser-secret",
                "Cookie": "ava_session=browser-cookie; grafana_session=spoof",
                "X-Ava-Grafana-User": "admin",
                "X-Ava-Grafana-Role": "Admin",
            },
        )
    assert resp.status_code == 200
    upstream = _UPSTREAM_REQUESTS[-1]
    assert upstream["x-ava-grafana-user"] == "ava-cluster-viewer"
    assert upstream["x-ava-grafana-role"] == "Viewer"
    assert "authorization" not in upstream
    assert "cookie" not in upstream


def test_proxy_allows_viewer_query_but_rejects_mutation_before_upstream(
    grafana_server: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable(monkeypatch, grafana_server)
    with TestClient(app) as client:
        read = client.post("/grafana/api/ds/query", json={"queries": []})
        write = client.post("/grafana/api/dashboards/db", json={"dashboard": {}})
        put = client.put("/grafana/api/dashboards/uid/x", json={})
        delete = client.delete("/grafana/api/dashboards/uid/x")
    assert read.status_code == 200
    assert write.status_code == 403
    assert put.status_code == 405
    assert delete.status_code == 405


def test_proxy_rewrites_only_same_upstream_redirects(
    grafana_server: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable(monkeypatch, grafana_server)
    with TestClient(app) as client:
        safe = client.get("/grafana/redirect-safe", follow_redirects=False)
        evil = client.get("/grafana/redirect-evil", follow_redirects=False)
    assert safe.status_code == 302
    assert safe.headers["location"] == "/grafana/d/ava-ops-main"
    assert evil.status_code == 502
    assert "unsafe redirect" in evil.text


def test_proxy_never_forwards_grafana_cookie(
    grafana_server: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable(monkeypatch, grafana_server)
    with TestClient(app) as client:
        resp = client.get("/grafana/cookie")
    assert resp.status_code == 200
    assert "set-cookie" not in resp.headers


def test_proxy_forwards_head(grafana_server: int, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch, grafana_server)
    with TestClient(app) as client:
        resp = client.head("/grafana/")
    assert resp.status_code == 200


def test_proxy_handles_gzip_upstream(grafana_server: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """The proxy asks for an identity-encoded body; an upstream that ignores
    that and gzips anyway still yields a correctly decoded body with no stale
    Content-Encoding / Content-Length headers (httpx auto-decodes, and the
    proxy drops the mismatched pair)."""
    _enable(monkeypatch, grafana_server)
    with TestClient(app) as client:
        resp = client.get("/grafana/gzip")
    assert resp.status_code == 200
    assert "content-encoding" not in resp.headers
    assert "content-length" not in resp.headers
    assert "<h1>compressed</h1>" in resp.text


def test_only_confirmed_event_stream_can_wait_quietly(
    grafana_server: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Caller-controlled Accept cannot turn an ordinary quiet response into an
    unbounded pool occupant; a response Grafana identifies as SSE can be quiet."""
    _enable(monkeypatch, grafana_server)
    monkeypatch.setattr(grafana, "_CHUNK_TIMEOUT_SECONDS", 0.01)
    with TestClient(app) as client:
        ordinary = client.get(
            "/grafana/quiet-json",
            headers={"Accept": "text/event-stream"},
        )
        event_stream = client.get("/grafana/quiet-sse")
    assert ordinary.status_code == 200
    assert ordinary.content == b""
    assert event_stream.content == b"data: ready\n\n"


@pytest.mark.parametrize("content_type", ["application/json", "text/event-stream"])
def test_response_start_disconnect_closes_unstarted_upstream(
    monkeypatch: pytest.MonkeyPatch,
    content_type: str,
) -> None:
    """Cleanup is owned by the response call, not first body iteration: a
    disconnect while sending headers closes the upstream and releases SSE
    capacity even though the body generator was never started."""

    class _TrackingStream(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.closed = False

        async def __aiter__(self):  # pyright: ignore[reportMissingTypeArgument]
            yield b"never reached"

        async def aclose(self) -> None:
            self.closed = True

    async def scenario() -> None:
        tracker = _TrackingStream()

        def upstream(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"Content-Type": content_type},
                stream=tracker,
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        app_state = SimpleNamespace(grafana_client=client)
        scope = cast(
            Scope,
            {
                "type": "http",
                "asgi": {"spec_version": "2.4"},
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": "/grafana/disconnect",
                "raw_path": b"/grafana/disconnect",
                "query_string": b"",
                "headers": [],
                "client": ("127.0.0.1", 1234),
                "server": ("testserver", 8000),
                "app": SimpleNamespace(state=app_state),
            },
        )

        async def receive() -> dict[str, object]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def fail_on_start(_message: dict[str, object]) -> None:
            raise OSError("client disconnected before headers")

        sse_baseline = grafana.capacity.reserved["sse"]
        http_baseline = grafana.capacity.reserved["http"]
        try:
            response = await grafana._proxy(  # pyright: ignore[reportPrivateUsage]
                "disconnect",
                Request(scope, receive),
            )
            with pytest.raises(ClientDisconnect):
                await response(scope, receive, fail_on_start)  # pyright: ignore[reportArgumentType]
            assert tracker.closed
            assert grafana.capacity.reserved["sse"] == sse_baseline
            assert grafana.capacity.reserved["http"] == http_baseline
        finally:
            await client.aclose()

    monkeypatch.setattr(config.settings.gateway, "grafana_proxy_enabled", True)
    monkeypatch.setattr(config.settings.gateway, "grafana_host", "127.0.0.1")
    monkeypatch.setattr(config.settings.gateway, "grafana_port", 3003)
    asyncio.run(scenario())


def test_proxy_502_when_grafana_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enabled but nothing listening on the upstream port surfaces as 502."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    free_port = probe.getsockname()[1]
    probe.close()
    _enable(monkeypatch, free_port)
    with TestClient(app) as client:
        resp = client.get("/grafana/")
    assert resp.status_code == 502


@pytest.mark.parametrize(
    "bad_path",
    [
        "%2e%2e/etc/passwd",  # ../
        "public/%2e%2e/%2e%2e/etc/passwd",  # ../../
        "%2e%2e%2fetc/passwd",  # encoded ../ without slash split
        "%2e%2e%5cetc/passwd",  # ..\ (Windows separator)
    ],
)
def test_proxy_rejects_traversal(
    grafana_server: int, monkeypatch: pytest.MonkeyPatch, bad_path: str
) -> None:
    """Encoded dot-segments and backslashes are rejected with 400 — the
    gateway never forwards a path that could address a different Grafana
    resource than the browser asked for."""
    _enable(monkeypatch, grafana_server)
    with TestClient(app) as client:
        resp = client.get(f"/grafana/{bad_path}")
    assert resp.status_code == 400


def test_proxy_requires_auth(grafana_server: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """The proxy is auth-gated like every other API route: no credentials ->
    401; a valid bearer passes the middleware (404 here because the proxy is
    disabled, proving it reached the route)."""
    monkeypatch.setattr(config.settings.gateway, "auth_middleware_enabled", True)
    monkeypatch.setattr(config.settings.data_plane, "cluster_secret", _SECRET)
    monkeypatch.setattr(config.settings.gateway, "grafana_proxy_enabled", False)
    with TestClient(app) as client:
        no_auth = client.get("/grafana/")
        assert no_auth.status_code == 401

        root_without_slash = client.get("/grafana", follow_redirects=False)
        assert root_without_slash.status_code == 401

        with_auth = client.get(
            "/grafana/",
            headers={"Authorization": f"Bearer {_SECRET}"},
        )
        assert with_auth.status_code == 404


def test_proxy_accepts_bearer_for_read(
    grafana_server: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable(monkeypatch, grafana_server)
    monkeypatch.setattr(config.settings.gateway, "auth_middleware_enabled", True)
    monkeypatch.setattr(config.settings.data_plane, "cluster_secret", _SECRET)
    with TestClient(app) as client:
        response = client.get(
            "/grafana/api/search",
            headers={"Authorization": f"Bearer {_SECRET}"},
        )
    assert response.status_code == 200


def test_proxy_bounds_request_body_before_dialing_upstream(
    grafana_server: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable(monkeypatch, grafana_server)
    _UPSTREAM_REQUESTS.clear()
    with TestClient(app) as client:
        response = client.post(
            "/grafana/api/ds/query",
            content=b"x" * (2 * 1024 * 1024 + 1),
        )
    assert response.status_code == 413
    assert _UPSTREAM_REQUESTS == []


def test_proxy_accepts_gateway_session_cookie(
    grafana_server: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable(monkeypatch, grafana_server)
    monkeypatch.setattr(config.settings.gateway, "auth_middleware_enabled", True)
    monkeypatch.setattr(config.settings.data_plane, "cluster_secret", _SECRET)
    with TestClient(app) as client:
        client.cookies.set(cookie_name(), sign_session(_SECRET))
        response = client.get("/grafana/api/search")
    assert response.status_code == 200


@dataclass
class _WsObservation:
    headers: dict[str, str] = field(default_factory=dict)
    path: str = ""
    closed_code: int | None = None


@pytest.fixture
def grafana_ws_server() -> Iterator[tuple[int, _WsObservation]]:
    observation = _WsObservation()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.listen()

    def handler(connection: ServerConnection) -> None:
        request = connection.request
        assert request is not None
        observation.headers = {k.lower(): v for k, v in request.headers.items()}
        observation.path = request.path
        try:
            while True:
                message = connection.recv()
                if message == "close-upstream":
                    connection.close(code=4001, reason="upstream restart")
                    return
                connection.send(message)
        except Exception:
            observation.closed_code = connection.close_code

    server = serve(handler, sock=sock, origins=None)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield port, observation
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_live_websocket_requires_session_and_configured_gateway_origin(
    grafana_ws_server: tuple[int, _WsObservation], monkeypatch: pytest.MonkeyPatch
) -> None:
    port, _observation = grafana_ws_server
    _enable(monkeypatch, port)
    monkeypatch.setattr(config.settings.gateway, "auth_middleware_enabled", True)
    monkeypatch.setattr(config.settings.data_plane, "cluster_secret", _SECRET)
    with TestClient(app) as client:
        with (
            pytest.raises(WebSocketDisconnect) as unauthenticated,
            client.websocket_connect(
                "/grafana/api/live/ws",
                headers={"Origin": "http://testserver:8000"},
            ),
        ):
            pass
        assert unauthenticated.value.code == 1008
        client.cookies.set(cookie_name(), sign_session(_SECRET))
        with (
            pytest.raises(WebSocketDisconnect) as wrong_origin,
            client.websocket_connect(
                "/grafana/api/live/ws",
                headers={"Origin": "https://evil.example"},
            ),
        ):
            pass
        assert wrong_origin.value.code == 1008


def test_live_websocket_origin_allows_only_configured_gateway_origin(
    grafana_ws_server: tuple[int, _WsObservation], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Origin includes scheme and effective port. The Grafana document is
    served by the gateway, so the loopback frontend health URL is irrelevant;
    another page on the gateway host cannot spend the ambient Ava cookie."""
    port, _observation = grafana_ws_server
    _enable(monkeypatch, port)
    monkeypatch.setattr(
        config.settings.services,
        "frontend_healthcheck_url",
        "http://localhost:3000",
    )
    with TestClient(app) as client:
        with (
            pytest.raises(WebSocketDisconnect) as wrong_port,
            client.websocket_connect(
                "/grafana/api/live/ws",
                headers={"Origin": "http://testserver:3001"},
            ),
        ):
            pass
        assert wrong_port.value.code == 1008

        with client.websocket_connect(
            "/grafana/api/live/ws",
            headers={"Origin": "http://testserver:8000"},
        ) as websocket:
            websocket.send_text("gateway-origin")
            assert websocket.receive_text() == "gateway-origin"


def test_live_websocket_relays_text_binary_and_close(
    grafana_ws_server: tuple[int, _WsObservation], monkeypatch: pytest.MonkeyPatch
) -> None:
    port, observation = grafana_ws_server
    _enable(monkeypatch, port)
    monkeypatch.setattr(config.settings.gateway, "auth_middleware_enabled", True)
    monkeypatch.setattr(config.settings.data_plane, "cluster_secret", _SECRET)
    with TestClient(app) as client:
        client.cookies.set(cookie_name(), sign_session(_SECRET))
        with client.websocket_connect(
            "/grafana/api/live/ws?orgId=1",
            headers={"Origin": "http://testserver:8000"},
        ) as websocket:
            websocket.send_text("hello")
            assert websocket.receive_text() == "hello"
            websocket.send_bytes(b"\x00\x01")
            assert websocket.receive_bytes() == b"\x00\x01"
            websocket.close(code=1000)
    assert observation.path == "/grafana/api/live/ws?orgId=1"
    assert observation.headers["x-ava-grafana-user"] == "ava-cluster-viewer"
    assert observation.headers["x-ava-grafana-role"] == "Viewer"
    assert "cookie" not in observation.headers
    assert "authorization" not in observation.headers


def test_live_websocket_rejects_secret_query(
    grafana_ws_server: tuple[int, _WsObservation], monkeypatch: pytest.MonkeyPatch
) -> None:
    port, _observation = grafana_ws_server
    _enable(monkeypatch, port)
    with TestClient(app) as client:
        with (
            pytest.raises(WebSocketDisconnect) as rejected,
            client.websocket_connect(
                "/grafana/api/live/ws?token=do-not-put-secrets-here",
                headers={"Origin": "http://testserver:8000"},
            ),
        ):
            pass
        assert rejected.value.code == 1008


def test_live_websocket_relays_upstream_close(
    grafana_ws_server: tuple[int, _WsObservation], monkeypatch: pytest.MonkeyPatch
) -> None:
    port, _observation = grafana_ws_server
    _enable(monkeypatch, port)
    with (
        TestClient(app) as client,
        client.websocket_connect(
            "/grafana/api/live/ws",
            headers={"Origin": "http://testserver:8000"},
        ) as websocket,
    ):
        websocket.send_text("close-upstream")
        with pytest.raises(WebSocketDisconnect) as closed:
            websocket.receive_text()
    assert closed.value.code == 4001


def test_live_websocket_relays_normal_upstream_close() -> None:
    """websockets iteration ends normally for 1000/1001; the bridge must
    still send the downstream close frame instead of leaving it hanging."""

    class _Socket:
        client_state = WebSocketState.CONNECTED
        application_state = WebSocketState.CONNECTED

        def __init__(self) -> None:
            self.close_calls: list[tuple[int, str]] = []

        async def close(self, code: int, reason: str) -> None:
            self.close_calls.append((code, reason))

    class _Upstream:
        close_code = 1000
        close_reason = "normal done"

        def __aiter__(self):  # pyright: ignore[reportMissingTypeArgument]
            async def empty():
                if False:
                    yield ""

            return empty()

    socket = _Socket()
    asyncio.run(
        grafana._grafana_to_client(  # pyright: ignore[reportArgumentType, reportPrivateUsage]
            socket,  # pyright: ignore[reportArgumentType]
            _Upstream(),  # pyright: ignore[reportArgumentType]
        )
    )
    assert socket.close_calls == [(1000, "normal done")]


def test_live_websocket_capacity_counts_pending_upstream_handshakes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slow upstream handshakes consume capacity before the first await, so a
    concurrent burst cannot all pass a stale ``len(active)`` check."""

    class _Url:
        query = ""

    class _Socket:
        def __init__(self) -> None:
            self.headers = {
                "host": "testserver:8000",
                "origin": "http://testserver:8000",
            }
            self.cookies: dict[str, str] = {}
            self.url = _Url()
            self.client_state = WebSocketState.CONNECTED
            self.application_state = WebSocketState.CONNECTED
            self.close_calls: list[tuple[int, str]] = []
            self.accepted = False

        async def close(self, code: int, reason: str) -> None:
            self.close_calls.append((code, reason))

        async def accept(self, subprotocol: str | None = None) -> None:
            self.accepted = True

    class _Upstream:
        subprotocol = None

    async def scenario() -> None:
        release = asyncio.Event()
        enough_pending = asyncio.Event()
        entered = 0

        class _SlowConnect:
            async def __aenter__(self) -> _Upstream:
                nonlocal entered
                entered += 1
                if entered >= grafana.capacity.WEBSOCKET_LIMIT:
                    enough_pending.set()
                await release.wait()
                return _Upstream()

            async def __aexit__(self, *_args: object) -> None:
                return None

        def slow_connect(*_args: object, **_kwargs: object) -> _SlowConnect:
            return _SlowConnect()

        async def relay_immediately(*_args: object) -> None:
            return None

        monkeypatch.setattr(config.settings.gateway, "grafana_proxy_enabled", True)
        monkeypatch.setattr(config.settings.gateway, "grafana_host", "127.0.0.1")
        monkeypatch.setattr(config.settings.gateway, "gateway_url", "http://testserver:8000")
        monkeypatch.setattr(config.settings.gateway, "auth_middleware_enabled", False)
        monkeypatch.setattr(grafana, "connect", slow_connect)
        monkeypatch.setattr(grafana, "_relay_websocket", relay_immediately)
        sockets = [_Socket() for _ in range(grafana.capacity.WEBSOCKET_LIMIT + 1)]
        tasks = [
            asyncio.create_task(grafana.grafana_live(socket))  # pyright: ignore[reportArgumentType]
            for socket in sockets
        ]
        await asyncio.wait_for(enough_pending.wait(), timeout=1)
        await asyncio.sleep(0)
        assert entered == grafana.capacity.WEBSOCKET_LIMIT
        assert sockets[-1].close_calls == [(1013, "grafana live capacity reached")]
        release.set()
        await asyncio.gather(*tasks)

    asyncio.run(scenario())


def test_gateway_restart_closes_live_websockets() -> None:
    class _Socket:
        def __init__(self) -> None:
            self.calls: list[tuple[int, str]] = []
            self.application_state = WebSocketState.CONNECTED

        async def close(self, code: int, reason: str) -> None:
            self.calls.append((code, reason))

    socket_one = _Socket()
    socket_two = _Socket()
    grafana._live_websockets.update({socket_one, socket_two})  # pyright: ignore[reportArgumentType, reportPrivateUsage]
    asyncio.run(grafana.close_live_websockets())
    assert socket_one.calls == [(1012, "gateway restarting")]
    assert socket_two.calls == [(1012, "gateway restarting")]
    assert not grafana._live_websockets  # pyright: ignore[reportPrivateUsage]
