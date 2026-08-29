"""Gateway Grafana reverse-proxy tests.

`/grafana/*` forwards to a co-located Grafana instance (host/port from
AVA_GRAFANA_HOST / AVA_GRAFANA_PORT) behind the cluster auth middleware,
streaming the upstream response. Covers:
- disabled by default: 404 on /grafana/*, nothing else changes
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

import gzip
import http.server
import json
import socket
import socketserver
import threading
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from shared import config

_SECRET = "test-cluster-secret"  # noqa: S105 — test fixture


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
        if path.startswith("/api/search"):
            self._send(200, b'{"dashboards": []}', "application/json")
        elif path.startswith("/gzip"):
            body = gzip.compress(b"<h1>compressed</h1>")
            self._send(200, body, "text/html", {"Content-Encoding": "gzip"})
        elif path.startswith("/"):
            self._send(200, b"<h1>grafana</h1>", "text/html")

    def do_POST(self) -> None:
        assert self.path.startswith("/grafana/"), f"prefix must be preserved, got {self.path!r}"
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
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def _enable(monkeypatch: pytest.MonkeyPatch, port: int) -> None:
    monkeypatch.setattr(config.settings.gateway, "grafana_proxy_enabled", True)
    monkeypatch.setattr(config.settings.gateway, "grafana_host", "127.0.0.1")
    monkeypatch.setattr(config.settings.gateway, "grafana_port", port)


def test_proxy_disabled_by_default(grafana_server: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """The proxy is off unless AVA_GRAFANA_PROXY_ENABLED — a cluster without
    Grafana gets 404 on /grafana/* and nothing else."""
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


def test_proxy_rejects_body_over_cap(grafana_server: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """Forwarded request buffering has a hard cap, unlike streamed responses."""
    from gateway.routers import grafana

    _enable(monkeypatch, grafana_server)
    monkeypatch.setattr(grafana, "_MAX_PROXY_REQUEST_BODY_BYTES", 8)
    with TestClient(app) as client:
        response = client.post("/grafana/api/ds/query", content=b"123456789")
    assert response.status_code == 413
    assert "exceeds 16 MiB" in response.json()["detail"]


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


def test_proxy_cookie_post_from_gateway_origin_passes(
    grafana_server: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end guard for the reported 403: a browser session (cookie)
    POSTing Grafana's datasource query from the gateway's own origin — the
    page is served at AVA_GATEWAY_URL — must reach the proxy, not be rejected
    by the exact-origin check. The derived allowlist used to carry the
    frontend port on the gateway host, so the gateway's own origin never
    matched and every same-origin data POST died with 403."""
    _enable(monkeypatch, grafana_server)
    monkeypatch.setattr(config.settings.gateway, "auth_middleware_enabled", True)
    monkeypatch.setattr(config.settings.data_plane, "cluster_secret", _SECRET)
    monkeypatch.setattr(config.settings.gateway, "cors_allowed_origins", [])
    monkeypatch.setattr(
        config.settings.services,
        "frontend_healthcheck_url",
        "http://localhost:3100",
    )
    monkeypatch.setattr(
        config.settings.gateway,
        "gateway_url",
        "http://100.103.96.72:8000",
    )
    body = {"queries": [{"refId": "A"}]}
    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"password": _SECRET})
        assert login.status_code == 200
        resp = client.post(
            "/grafana/api/ds/query",
            json=body,
            headers={"Origin": "http://100.103.96.72:8000"},
        )

    assert resp.status_code == 200
    assert json.loads(resp.json()["echo"]) == body


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

        with_auth = client.get(
            "/grafana/",
            headers={"Authorization": f"Bearer {_SECRET}"},
        )
        assert with_auth.status_code == 404
