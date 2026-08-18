"""End-to-end behavior of the always-up gate, against real loopback sockets.

The gate's whole point is external behavior (what the browser sees during a
rollout), so these tests exercise the real HTTP path: a fake gateway (auth
check + 503 mode), a fake app, and the gate itself, all on ephemeral ports.
"""

from __future__ import annotations

import json
import threading
import types
import urllib.error
import urllib.request
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar, TypedDict

import pytest

from services.gate.daemon import Gate, _Handler

STATIC = Path(__file__).parent.parent.parent / "services" / "gate" / "static"


class _FakeGateway(BaseHTTPRequestHandler):
    authenticated = False
    down = False

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        if self.path == "/api/auth/check":
            if _FakeGateway.down:
                self.send_response(503)
                self.end_headers()
                return
            body = json.dumps({"authenticated": _FakeGateway.authenticated}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


class _FakeApp(BaseHTTPRequestHandler):
    not_found_paths: ClassVar[set[str]] = set()

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        if self.path in _FakeApp.not_found_paths:
            body = f"NOT-FOUND {self.path}".encode()
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = f"APP-PAGE {self.path}".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _Servers(TypedDict):
    gate: str
    gw: ThreadingHTTPServer
    app: ThreadingHTTPServer
    gate_server: ThreadingHTTPServer
    flag: Path


@pytest.fixture
def servers(tmp_path: Path) -> Iterator[_Servers]:
    gw = ThreadingHTTPServer(("127.0.0.1", 0), _FakeGateway)
    app = ThreadingHTTPServer(("127.0.0.1", 0), _FakeApp)
    gate_server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    # The deploy-state mirror, as a controllable file: a test writes
    # `{"posture": "paused"}` to make the gate say "updating", leaves it absent
    # to make the gate say "down" (R1, Task #1021 — replaces the updating.flag).
    mirror = tmp_path / "deploy-state.json"
    gate = Gate(
        gateway_base=f"http://127.0.0.1:{gw.server_port}",
        app_base=f"http://127.0.0.1:{app.server_port}",
        static_dir=STATIC,
        mirror_path=str(mirror),
    )
    gate_server.gate = gate  # type: ignore[attr-defined]
    for s in (gw, app, gate_server):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    yield {
        "gate": f"http://127.0.0.1:{gate_server.server_port}",
        "gw": gw,
        "app": app,
        "gate_server": gate_server,
        "flag": mirror,
    }
    for s in (gw, app, gate_server):
        s.shutdown()


def _get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 — test loopback
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_authenticated_proxies_app(servers) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    _FakeGateway.authenticated = True
    _FakeGateway.down = False
    status, body = _get(servers["gate"] + "/fleet")  # pyright: ignore[reportUnknownArgumentType]
    assert status == 200
    assert body == "APP-PAGE /fleet"


def test_app_404_passes_through_not_updating_page(servers) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    """The app answering 404 means the app is UP and the resource is gone — the
    gate must pass the 404 through. A browser probing /favicon.ico relies on
    the 404 to drop its cached icon; an updating page instead would keep the
    stale favicon alive indefinitely (the icon-resurrection bug)."""
    _FakeGateway.authenticated = True
    _FakeGateway.down = False
    _FakeApp.not_found_paths.add("/favicon.ico")
    try:
        status, body = _get(servers["gate"] + "/favicon.ico")  # pyright: ignore[reportUnknownArgumentType]
        assert status == 404
        assert body == "NOT-FOUND /favicon.ico"
        assert "System updating" not in body
    finally:
        _FakeApp.not_found_paths.clear()


def test_unauthenticated_serves_static_login(servers) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    _FakeGateway.authenticated = False
    _FakeGateway.down = False
    status, body = _get(servers["gate"] + "/")  # pyright: ignore[reportUnknownArgumentType]
    assert status == 200
    assert "Sign in" in body
    assert "login" in body.lower()


def test_gateway_503_serves_updating_page_when_flag_set(servers) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    """Phase A: the gateway 503s while the updating flag is set — the user must
    see the updating page, not a dead-app error."""
    _FakeGateway.authenticated = True
    _FakeGateway.down = True
    servers["flag"].write_text('{"posture": "paused"}')  # pyright: ignore[reportUnknownMemberType]
    status, body = _get(servers["gate"] + "/")  # pyright: ignore[reportUnknownArgumentType]
    assert status == 503
    assert "System updating" in body


def test_gateway_refused_serves_updating_page_when_flag_set(servers) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    """Local leg: the gateway process is down entirely (connection refused) but
    the rollout marked the flag — updating page."""
    _FakeGateway.authenticated = True
    _FakeGateway.down = False
    servers["flag"].write_text('{"posture": "paused"}')  # pyright: ignore[reportUnknownMemberType]
    servers["gw"].shutdown()  # pyright: ignore[reportUnknownMemberType]
    status, body = _get(servers["gate"] + "/")  # pyright: ignore[reportUnknownArgumentType]
    assert status == 503
    assert "System updating" in body


def test_gateway_503_without_flag_serves_down_page(servers) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    """Gateway 503, no flag: the machine itself is not answering — the down page,
    and its copy must NOT promise an update."""
    _FakeGateway.authenticated = True
    _FakeGateway.down = True
    assert not servers["flag"].exists()  # pyright: ignore[reportUnknownMemberType]
    status, body = _get(servers["gate"] + "/")  # pyright: ignore[reportUnknownArgumentType]
    assert status == 503
    assert "Service unavailable" in body
    assert "System updating" not in body


def test_gateway_refused_without_flag_serves_down_page(servers) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    """Connection refused, no flag: the machine is down (powered off / crashed),
    not mid-update."""
    _FakeGateway.authenticated = True
    servers["gw"].shutdown()  # pyright: ignore[reportUnknownMemberType]
    assert not servers["flag"].exists()  # pyright: ignore[reportUnknownMemberType]
    status, body = _get(servers["gate"] + "/")  # pyright: ignore[reportUnknownArgumentType]
    assert status == 503
    assert "Service unavailable" in body
    assert "System updating" not in body


def test_flag_cleared_between_requests_switches_the_page(servers) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    """The flag is read per request: a rollout that finishes (flag cleared) while
    the gateway is still down flips the page to "down" on the next refresh —
    and once the gateway answers again, the app comes back."""
    _FakeGateway.authenticated = True
    _FakeGateway.down = True
    servers["flag"].write_text('{"posture": "paused"}')  # pyright: ignore[reportUnknownMemberType]
    _, body = _get(servers["gate"] + "/")  # pyright: ignore[reportUnknownArgumentType]
    assert "System updating" in body
    servers["flag"].unlink()  # pyright: ignore[reportUnknownMemberType]
    _, body = _get(servers["gate"] + "/")  # pyright: ignore[reportUnknownArgumentType]
    assert "Service unavailable" in body


@pytest.mark.parametrize("page", ["login_page", "updating_page", "down_page"])
def test_pages_ship_self_contained(servers, page: str) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    """Every page carries its own styling and theme bootstrap.

    The gate answers one page per request and has no route to serve a second
    asset, so a page that referenced one — or that shipped with its marker
    unsubstituted — would render bare exactly when the app is down. Both the
    inlining and the absence of any outbound reference are load-bearing.
    """
    body = getattr(servers["gate_server"].gate, page)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    assert "/*__THEME_" not in body, "a theme marker survived load"
    assert "--background:" in body, "design tokens not inlined"
    assert "prefers-color-scheme" in body, "theme bootstrap not inlined"
    for external in ("<link ", "src=", "@import", "//fonts."):
        assert external not in body, f"page reaches outside itself: {external}"


def test_gateway_base_uses_reachable_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """The login page posts to the gateway origin, so the host must be one a
    remote browser can dial — the operator-declared reachable address, not
    loopback (a remote browser resolving 127.0.0.1 hits itself; regression for
    the gate login failure on remote machines). The server-side probe shares
    the same base. Port comes from settings.gateway.gateway_port.
    """
    from services.gate import daemon

    monkeypatch.setattr(
        daemon, "settings", types.SimpleNamespace(gateway=types.SimpleNamespace(gateway_port=8123))
    )
    monkeypatch.setattr("shared.machine.reachable_host", lambda: "100.64.0.2")
    assert daemon._gateway_base() == "http://100.64.0.2:8123"

    # Single box: the localhost fallback keeps local browsers working verbatim.
    monkeypatch.setattr("shared.machine.reachable_host", lambda: "localhost")
    assert daemon._gateway_base() == "http://localhost:8123"

    # IPv6 literals need brackets in a netloc.
    monkeypatch.setattr("shared.machine.reachable_host", lambda: "fd00::1")
    assert daemon._gateway_base() == "http://[fd00::1]:8123"
