"""End-to-end behavior of the always-up gate, against real loopback sockets.

The gate's whole point is external behavior (what the browser sees during a
rollout), so these tests exercise the real HTTP path: a fake gateway (auth
check + 503 mode), a fake app, and the gate itself, all on ephemeral ports.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import threading
import types
import urllib.error
import urllib.request
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar, TypedDict

import pytest

import services.gate.daemon as gate_daemon
from services.gate.daemon import Gate, _Handler

STATIC = Path(__file__).parent.parent.parent / "services" / "gate" / "static"


class _FakeGateway(BaseHTTPRequestHandler):
    authenticated = False
    down = False
    requests = 0
    auth_hook: ClassVar[object | None] = None
    # Failure-mode knobs for the auth-probe classification tests: `auth_status`
    # answers the probe with that HTTP status (no body); `raw_body` answers 200
    # with a literal body (garbage JSON). Both take precedence over `down`.
    auth_status: ClassVar[int | None] = None
    raw_body: ClassVar[str | None] = None

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        _FakeGateway.requests += 1
        if self.path == "/api/auth/check":
            hook = _FakeGateway.auth_hook
            if callable(hook):
                hook()
            if _FakeGateway.auth_status is not None:
                self.send_response(_FakeGateway.auth_status)
                self.end_headers()
                return
            if _FakeGateway.raw_body is not None:
                body = _FakeGateway.raw_body.encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
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
    forwarded_origin_headers: ClassVar[list[dict[str, str | None]]] = []
    received_hosts: ClassVar[list[str | None]] = []
    requests = 0

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        _FakeApp.requests += 1
        _FakeApp.received_hosts.append(self.headers.get("host"))
        _FakeApp.forwarded_origin_headers.append(
            {
                "x-forwarded-host": self.headers.get("x-forwarded-host"),
                "x-forwarded-proto": self.headers.get("x-forwarded-proto"),
            }
        )
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
        self.send_header("Content-Security-Policy", "default-src 'self'")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
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
    _FakeGateway.requests = 0
    _FakeGateway.auth_hook = None
    _FakeGateway.auth_status = None
    _FakeGateway.raw_body = None
    _FakeApp.requests = 0
    _FakeApp.forwarded_origin_headers = []
    _FakeApp.received_hosts = []
    gw = ThreadingHTTPServer(("127.0.0.1", 0), _FakeGateway)
    app = ThreadingHTTPServer(("127.0.0.1", 0), _FakeApp)
    gate_server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    # The Gate-owned marker is controllable here. A v1 marker exercises the
    # rollout introducing the v2 writer: paused means updating; absent means down.
    marker = tmp_path / "deploy-state.json"
    gate = Gate(
        gateway_base=f"http://127.0.0.1:{gw.server_port}",
        app_base=f"http://127.0.0.1:{app.server_port}",
        static_dir=STATIC,
        state_path=str(marker),
    )
    gate_server.gate = gate  # type: ignore[attr-defined]
    for s in (gw, app, gate_server):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    yield {
        "gate": f"http://127.0.0.1:{gate_server.server_port}",
        "gw": gw,
        "app": app,
        "gate_server": gate_server,
        "flag": marker,
    }
    for s in (gw, app, gate_server):
        s.shutdown()


@pytest.fixture
def probe_events() -> Iterator[list[dict[str, object]]]:
    """Capture `gate_auth_probe_failed` records the gate emits through the
    loguru logger — the structured half of the fail-closed verdict (audit
    #1736). The daemon's module-level `logger` is the shared.log singleton,
    so a sink on `loguru.logger` sees the records."""
    from loguru import logger

    seen: list[dict[str, object]] = []

    def sink(message: Any) -> None:
        rec = message.record
        if rec["extra"].get("event") == "gate_auth_probe_failed":
            seen.append(dict(rec["extra"]))

    sink_id = logger.add(sink, level="WARNING")
    try:
        yield seen
    finally:
        logger.remove(sink_id)


def _probe_failure_asserts(seen: list[dict[str, object]], *, category: str) -> dict[str, object]:
    """The probe failed fail-closed AND left exactly one classified event."""
    assert len(seen) == 1, f"expected one gate_auth_probe_failed event, got {seen!r}"
    event = seen[0]
    assert event["category"] == category
    assert isinstance(event["exception_type"], str) and event["exception_type"]
    assert isinstance(event["latency_ms"], int) and event["latency_ms"] >= 0
    return event


def _http_error(status: int) -> urllib.error.HTTPError:
    """One HTTPError with the shape urlopen raises for an error status."""
    return urllib.error.HTTPError("http://gw", status, "error", {}, None)  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (_http_error(401), "auth"),
        (_http_error(403), "auth"),
        (_http_error(404), "application"),
        (_http_error(503), "application"),
        (TimeoutError("timed out"), "timeout"),
        (urllib.error.URLError(TimeoutError("timed out")), "timeout"),
        (urllib.error.URLError(ConnectionRefusedError(61, "refused")), "network"),
        (ConnectionResetError(54, "reset"), "network"),
        (json.JSONDecodeError("Expecting value", "x", 0), "application"),
        (ValueError("boom"), "application"),
    ],
)
def test_classify_probe_error(exc: BaseException, expected: str) -> None:
    """Every auth-probe failure lands in exactly one of the four audit
    categories — auth / timeout / network / application — so a postmortem can
    tell the incident shapes apart from the event's category alone."""
    assert gate_daemon.classify_probe_error(exc) == expected


def test_probe_auth_failure_serves_down_and_emits_auth_event(
    servers: _Servers, probe_events: list[dict[str, object]]
) -> None:
    """The gateway answering 401/403 to the auth check is an AUTH failure —
    the route is auth-bypassed and always 200, so a 401/403 means the auth
    layer in front of it rejected the probe. External behavior stays the
    down page; internally the event says "auth"."""
    _FakeGateway.auth_status = 401

    status, body = _get(servers["gate"] + "/")

    assert status == 503
    assert "Service unavailable" in body
    event = _probe_failure_asserts(probe_events, category="auth")
    assert event["exception_type"] == "HTTPError"
    assert event["status"] == 401


def test_probe_timeout_serves_down_and_emits_timeout_event(
    servers: _Servers,
    probe_events: list[dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A probe that outlives its budget is a TIMEOUT, not a network outage —
    the distinction the audit needed (a slow gateway needs different surgery
    than a dead one)."""
    monkeypatch.setattr(gate_daemon, "_PROBE_TIMEOUT_S", 0.2)
    _FakeGateway.auth_hook = lambda: __import__("time").sleep(1.0)

    status, body = _get(servers["gate"] + "/")

    assert status == 503
    assert "Service unavailable" in body
    _probe_failure_asserts(probe_events, category="timeout")


def test_probe_network_failure_serves_down_and_emits_network_event(
    servers: _Servers, probe_events: list[dict[str, object]]
) -> None:
    """The gateway process gone (connection refused) is a NETWORK failure.

    `shutdown()` alone stops the accept loop but leaves the listening socket
    open — a fresh connect would queue and hang until the probe timeout — so
    `server_close()` is what makes the port actually refuse connections."""
    servers["gw"].shutdown()
    servers["gw"].server_close()

    status, body = _get(servers["gate"] + "/")

    assert status == 503
    assert "Service unavailable" in body
    _probe_failure_asserts(probe_events, category="network")


def test_probe_gateway_error_serves_down_and_emits_application_event(
    servers: _Servers, probe_events: list[dict[str, object]]
) -> None:
    """The gateway up but answering the check with an error status is an
    APPLICATION failure — the machine is reachable, the service is failing."""
    _FakeGateway.auth_status = 503

    status, body = _get(servers["gate"] + "/")

    assert status == 503
    assert "Service unavailable" in body
    event = _probe_failure_asserts(probe_events, category="application")
    assert event["status"] == 503


def test_probe_garbage_body_serves_down_and_emits_application_event(
    servers: _Servers, probe_events: list[dict[str, object]]
) -> None:
    """The gateway answering 200 with a non-JSON body is an APPLICATION
    failure — the check contract is broken even though the transport worked."""
    _FakeGateway.raw_body = "{not-json"

    status, body = _get(servers["gate"] + "/")

    assert status == 503
    assert "Service unavailable" in body
    event = _probe_failure_asserts(probe_events, category="application")
    assert event["exception_type"] == "JSONDecodeError"


def _get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 — test loopback
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def _request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
) -> tuple[int, str, dict[str, str]]:
    request = urllib.request.Request(  # noqa: S310 — loopback fixture
        url, headers=headers or {}, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as resp:  # noqa: S310 — loopback
            return resp.status, resp.read().decode(), dict(resp.headers.items())
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(), dict(exc.headers.items())


def _write_v1(path: Path, posture: str) -> None:
    path.write_text(json.dumps({"posture": posture, "updated_at": "2026-08-24T12:34:56+00:00"}))


def _write_v2(path: Path, *, kind: str = "rollout") -> None:
    started_at = "2026-08-24T12:34:56+00:00"
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "generation": f"{kind}-generation",
                "state": "updating",
                "kind": kind,
                "posture": "paused",
                "started_at": started_at,
                "updated_at": started_at,
                "phase": "phase-b",
                "origin": "test",
            }
        )
    )


def test_authenticated_proxies_app(servers) -> None:
    _FakeGateway.authenticated = True
    _FakeGateway.down = False
    status, body = _get(servers["gate"] + "/fleet")  # pyright: ignore[reportUnknownArgumentType]
    assert status == 200
    assert body == "APP-PAGE /fleet"


def test_app_proxy_forwards_browser_security_response_headers(servers: _Servers) -> None:
    _FakeGateway.authenticated = True
    _FakeGateway.down = False

    status, body, headers = _request(servers["gate"] + "/fleet")

    assert status == 200
    assert body == "APP-PAGE /fleet"
    assert headers["Content-Security-Policy"] == "default-src 'self'"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "strict-origin-when-cross-origin"


def test_app_proxy_preserves_browser_host_without_forwarded_origin_headers(
    servers: _Servers,
) -> None:
    _FakeGateway.authenticated = True
    _FakeGateway.down = False

    status, body, _ = _request(servers["gate"] + "/fleet")

    assert status == 200
    assert body == "APP-PAGE /fleet"
    assert _FakeApp.received_hosts == [servers["gate"].removeprefix("http://")]
    assert _FakeApp.forwarded_origin_headers == [
        {"x-forwarded-host": None, "x-forwarded-proto": None}
    ]


def test_app_proxy_forwards_browser_origin_headers_only_when_provided(servers: _Servers) -> None:
    """The Next.js CSP proxy needs the original browser origin, while callers
    without a TLS/proxy header retain the prior loopback behavior."""
    _FakeGateway.authenticated = True
    _FakeGateway.down = False

    status, body, _ = _request(
        servers["gate"] + "/fleet",
        headers={
            "X-Forwarded-Host": "console.example.test:3109",
            "X-Forwarded-Proto": "https",
        },
    )

    assert status == 200
    assert body == "APP-PAGE /fleet"
    assert _FakeApp.forwarded_origin_headers == [
        {
            "x-forwarded-host": "console.example.test:3109",
            "x-forwarded-proto": "https",
        }
    ]

    status, body, _ = _request(servers["gate"] + "/fleet")

    assert status == 200
    assert body == "APP-PAGE /fleet"
    assert _FakeApp.forwarded_origin_headers[-1] == {
        "x-forwarded-host": None,
        "x-forwarded-proto": None,
    }


def test_app_proxy_rejects_invalid_browser_origin_headers(servers: _Servers) -> None:
    _FakeGateway.authenticated = True
    _FakeGateway.down = False

    status, body, _ = _request(
        servers["gate"] + "/fleet",
        headers={
            "X-Forwarded-Host": "console.example.test/invalid",
            "X-Forwarded-Proto": "ftp",
        },
    )

    assert status == 200
    assert body == "APP-PAGE /fleet"
    assert _FakeApp.forwarded_origin_headers == [
        {"x-forwarded-host": None, "x-forwarded-proto": None}
    ]


def test_app_404_passes_through_not_updating_page(servers) -> None:
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


def test_unauthenticated_serves_static_login(servers) -> None:
    _FakeGateway.authenticated = False
    _FakeGateway.down = False
    status, body = _get(servers["gate"] + "/")  # pyright: ignore[reportUnknownArgumentType]
    assert status == 200
    assert "Sign in" in body
    assert "login" in body.lower()


def test_gateway_503_serves_updating_page_when_flag_set(servers) -> None:
    """Phase A: the gateway 503s while the updating flag is set — the user must
    see the updating page, not a dead-app error."""
    _FakeGateway.authenticated = True
    _FakeGateway.down = True
    _write_v1(servers["flag"], "paused")  # pyright: ignore[reportUnknownArgumentType]
    status, body = _get(servers["gate"] + "/")  # pyright: ignore[reportUnknownArgumentType]
    assert status == 503
    assert "System updating" in body


def test_active_snapshot_fast_path_never_probes_gateway_or_app(servers) -> None:
    _write_v2(servers["flag"])  # pyright: ignore[reportUnknownArgumentType]

    status, body = _get(servers["gate"] + "/")  # pyright: ignore[reportUnknownArgumentType]

    assert status == 503
    assert "System updating" in body
    assert _FakeGateway.requests == 0
    assert _FakeApp.requests == 0


def test_one_request_reuses_its_initial_snapshot_when_file_changes_during_auth(servers) -> None:
    _FakeGateway.authenticated = True
    _FakeGateway.down = False
    _FakeGateway.auth_hook = lambda: _write_v2(servers["flag"])  # pyright: ignore[reportUnknownArgumentType]
    servers["app"].shutdown()  # pyright: ignore[reportUnknownMemberType]

    status, body = _get(servers["gate"] + "/")  # pyright: ignore[reportUnknownArgumentType]
    assert status == 503
    assert "Service unavailable" in body
    assert "System updating" not in body

    # Only the next request may observe the newly committed generation.
    _FakeGateway.auth_hook = None
    status, body = _get(servers["gate"] + "/")  # pyright: ignore[reportUnknownArgumentType]
    assert status == 503
    assert "System updating" in body


def test_same_origin_snapshot_hint_is_served_without_gateway_or_app(servers) -> None:
    _write_v2(servers["flag"], kind="rollout")  # pyright: ignore[reportUnknownArgumentType]

    status, body, headers = _request(servers["gate"] + "/__ava/deploy-state")  # pyright: ignore[reportUnknownArgumentType]

    assert status == 200
    assert json.loads(body) == {
        "status": "updating",
        "generation": "rollout-generation",
    }
    assert headers["Cache-Control"] == "no-store"
    assert headers["Content-Type"] == "application/json"
    assert _FakeGateway.requests == 0
    assert _FakeApp.requests == 0


def test_same_origin_snapshot_hint_is_get_only_and_never_probes(servers: _Servers) -> None:
    status, body, headers = _request(servers["gate"] + "/__ava/deploy-state", method="POST")

    assert status == 405
    assert body == ""
    assert headers["Allow"] == "GET"
    assert _FakeGateway.requests == 0
    assert _FakeApp.requests == 0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, {"status": "inactive", "generation": None}), ("{bad-json", None)],
)
def test_same_origin_snapshot_hint_projects_inactive_and_invalid_without_probes(
    servers: _Servers,
    raw: str | None,
    expected: dict[str, object] | None,
) -> None:
    if raw is not None:
        servers["flag"].write_text(raw)

    status, body = _get(servers["gate"] + "/__ava/deploy-state")

    assert status == 200
    payload = json.loads(body)
    if expected is None:
        assert payload["status"] == "invalid"
        assert payload["generation"] is None
    else:
        assert payload == expected
    assert _FakeGateway.requests == 0
    assert _FakeApp.requests == 0


def test_updating_page_uses_the_flags_stable_started_at(
    servers: _Servers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The static page must reconstruct elapsed time from the persisted update
    snapshot; browser storage/new tabs are not deployment state."""
    _FakeGateway.authenticated = True
    _FakeGateway.down = True
    started = dt.datetime(2026, 8, 24, 12, 34, 56, tzinfo=dt.UTC)
    fixed_now = started + dt.timedelta(seconds=75)

    class _FixedDateTime(dt.datetime):
        @classmethod
        def now(cls, tz: dt.tzinfo | None = None) -> dt.datetime:
            return fixed_now if tz is not None else fixed_now.replace(tzinfo=None)

    monkeypatch.setattr(gate_daemon.dt, "datetime", _FixedDateTime)
    started_at = started.isoformat()
    servers["flag"].write_text(
        json.dumps(
            {
                "schema_version": 2,
                "generation": started_at,
                "state": "updating",
                "kind": "rollout",
                "posture": "paused",
                "started_at": started_at,
                "updated_at": started_at,
                "phase": "phase-b",
                "origin": "test",
            }
        )
    )
    status, body = _get(servers["gate"] + "/")
    assert status == 503
    assert f'window.__AVA_DEPLOY_STARTED_AT__ = "{started_at}"' in body
    match = re.search(r"window\.__AVA_UPDATE_BASE_ELAPSED_MS__ = (\d+);", body)
    assert match is not None
    assert int(match.group(1)) == 75_000
    assert "/*__AVA_" not in body
    assert "sessionStorage" not in body


def test_gateway_refused_serves_updating_page_when_flag_set(servers) -> None:
    """Local leg: the gateway process is down entirely (connection refused) but
    the rollout marked the flag — updating page."""
    _FakeGateway.authenticated = True
    _FakeGateway.down = False
    _write_v1(servers["flag"], "paused")  # pyright: ignore[reportUnknownArgumentType]
    servers["gw"].shutdown()  # pyright: ignore[reportUnknownMemberType]
    status, body = _get(servers["gate"] + "/")  # pyright: ignore[reportUnknownArgumentType]
    assert status == 503
    assert "System updating" in body


def test_gateway_503_without_flag_serves_down_page(servers) -> None:
    """Gateway 503, no flag: the machine itself is not answering — the down page,
    and its copy must NOT promise an update."""
    _FakeGateway.authenticated = True
    _FakeGateway.down = True
    assert not servers["flag"].exists()  # pyright: ignore[reportUnknownMemberType]
    status, body = _get(servers["gate"] + "/")  # pyright: ignore[reportUnknownArgumentType]
    assert status == 503
    assert "Service unavailable" in body
    assert "System updating" not in body


def test_gateway_refused_without_flag_serves_down_page(servers) -> None:
    """Connection refused, no flag: the machine is down (powered off / crashed),
    not mid-update."""
    _FakeGateway.authenticated = True
    servers["gw"].shutdown()  # pyright: ignore[reportUnknownMemberType]
    assert not servers["flag"].exists()  # pyright: ignore[reportUnknownMemberType]
    status, body = _get(servers["gate"] + "/")  # pyright: ignore[reportUnknownArgumentType]
    assert status == 503
    assert "Service unavailable" in body
    assert "System updating" not in body


def test_each_failed_request_uses_the_current_persisted_snapshot(servers) -> None:
    """The gate may change pages only when the persisted deploy snapshot changes;
    a transport phase never invents a second state."""
    _FakeGateway.authenticated = True
    _FakeGateway.down = True
    _write_v1(servers["flag"], "paused")  # pyright: ignore[reportUnknownArgumentType]
    _, body = _get(servers["gate"] + "/")  # pyright: ignore[reportUnknownArgumentType]
    assert "System updating" in body
    servers["flag"].unlink()  # pyright: ignore[reportUnknownMemberType]
    _, body = _get(servers["gate"] + "/")  # pyright: ignore[reportUnknownArgumentType]
    assert "Service unavailable" in body


def test_app_transport_failure_without_active_marker_is_service_unavailable(servers) -> None:
    """Gateway-up/app-down is the exact recovery gap observed in production.

    It must reuse the request's inactive snapshot; app transport failure cannot
    invent System Updating after the gateway auth probe succeeded.
    """
    _FakeGateway.authenticated = True
    _FakeGateway.down = False
    assert not servers["flag"].exists()  # pyright: ignore[reportUnknownMemberType]
    servers["app"].shutdown()  # pyright: ignore[reportUnknownMemberType]

    status, body = _get(servers["gate"] + "/")  # pyright: ignore[reportUnknownArgumentType]
    assert status == 503
    assert "Service unavailable" in body
    assert "System updating" not in body
    assert _FakeGateway.requests == 1


@pytest.mark.parametrize(
    "raw",
    [
        "{not-json",
        '{"schema_version":2,"state":"mystery","started_at":null}',
        '{"schema_version":2,"state":"updating","started_at":"not-a-time"}',
    ],
)
def test_malformed_or_unknown_flag_fails_to_service_unavailable(
    servers: _Servers,
    raw: str,
) -> None:
    """Corrupt/unknown state is never guessed to mean an update. The gate stays
    available, reports Not Working, and leaves an observable warning."""
    _FakeGateway.authenticated = True
    _FakeGateway.down = True
    servers["flag"].write_text(raw)

    status, body = _get(servers["gate"] + "/")
    assert status == 503
    assert "Service unavailable" in body
    assert "No update is in progress" not in body
    assert "System updating" not in body


@pytest.mark.parametrize("page", ["login_page", "updating_page", "down_page"])
def test_pages_ship_self_contained(servers, page: str) -> None:
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
    monkeypatch.setattr("shared.machine.reachable_host", lambda: "10.0.0.2")
    assert daemon._gateway_base() == "http://10.0.0.2:8123"

    # Single box: the localhost fallback keeps local browsers working verbatim.
    monkeypatch.setattr("shared.machine.reachable_host", lambda: "localhost")
    assert daemon._gateway_base() == "http://localhost:8123"

    # IPv6 literals need brackets in a netloc.
    monkeypatch.setattr("shared.machine.reachable_host", lambda: "fd00::1")
    assert daemon._gateway_base() == "http://[fd00::1]:8123"
