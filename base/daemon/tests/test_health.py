"""`base.daemon.health` — minimal asyncio HTTP server.

Validates:
- GET /healthz → 200 + JSON includes name / pid / home / started_at
- Other paths → 404
- start server + stop pair is idempotent (stop called twice does not raise)
- _health_port: env override takes priority, defaults to DEFAULT_PORTS, unregistered raises KeyError
- probe_daemon: 200 only counts as alive when the name/home/pid triple matches
- probe_home: the pid-less sibling for `/api/health`, where the reload fork makes
  a pid comparison meaningless — 200 plus this unit's `$AVA_HOME`
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import cast

import pytest

from base.daemon import health
from base.daemon.health_schema import DEGRADED, OK, component
from base.daemon.tests._health_helpers import _find_free_port, _http_get
from base.daemon.tests.health_support import token_digest, unknown_image


@pytest.mark.asyncio
async def test_healthz_returns_200_with_json_body() -> None:
    port = _find_free_port()
    server = await health.start_health_server("agent_host", port=port, image=unknown_image())
    try:
        status, body = await _http_get(port, "/healthz")
        assert status == 200
        payload = json.loads(body)
        assert payload["name"] == "agent_host"
        assert isinstance(payload["pid"], int)
        assert isinstance(payload["started_at"], float)
        assert payload["status"] == "ok"
        assert payload["readiness"] == "ok"
        assert payload["components"] == [{"name": "loop", "status": "ok"}]
        assert payload["degraded_reasons"] == []
    finally:
        await health.stop_health_server(server)


@pytest.mark.asyncio
async def test_liveness_flips_stale_then_fresh_on_beat() -> None:
    """Liveness: fresh at construction; goes stale once timeout elapses with no
    beat; beat() resets it to fresh."""
    lv = health.Liveness(timeout_s=0.05)
    assert lv.is_alive()
    await asyncio.sleep(0.15)
    assert not lv.is_alive()
    assert lv.stale_for() >= 0.15
    lv.beat()
    assert lv.is_alive()


def test_loop_progress_flips_stale_then_fresh_on_completed_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A loop becomes stale after its own deadline; another completed unit resets it."""
    now = 100.0
    monkeypatch.setattr(health.time, "monotonic", lambda: now)
    progress = health.LoopProgress("dispatch", timeout_s=5.0)

    assert progress.name == "dispatch"
    assert progress.timeout_s == 5.0
    assert progress.is_alive()

    now = 106.0
    assert not progress.is_alive()
    assert progress.stale_for() == 6.0

    progress.beat()
    assert progress.is_alive()
    assert progress.stale_for() == 0.0


def test_loop_progress_snapshot_records_success_error_and_permanent_wedge() -> None:
    """Fail records the reason and permanently wins over later sibling-style beats."""
    progress = health.LoopProgress("resolution", timeout_s=60.0)
    progress.mark_success()
    progress.mark_error("loki unavailable")

    before_wedge = progress.snapshot()
    assert before_wedge["name"] == "resolution"
    assert isinstance(before_wedge["stale_for"], float)
    assert datetime.fromisoformat(cast(str, before_wedge["last_success_at"]))
    last_error = cast(dict[str, str], before_wedge["last_error"])
    assert last_error["message"] == "loki unavailable"
    assert datetime.fromisoformat(last_error["at"])
    assert before_wedge["wedged"] is False

    progress.fail("resolution exceeded hard deadline")
    progress.beat()
    assert not progress.is_alive()
    wedged_error = cast(dict[str, str], progress.snapshot()["last_error"])
    assert wedged_error["message"] == "resolution exceeded hard deadline"
    assert progress.snapshot()["wedged"] is True


def test_liveness_group_reports_the_worst_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh sibling cannot hide a loop whose own progress age exceeds its deadline."""
    now = 10.0
    monkeypatch.setattr(health.time, "monotonic", lambda: now)
    group = health.LivenessGroup()
    dispatch = group.register("dispatch", timeout_s=5.0)

    now = 13.0
    trim = group.register("trim", timeout_s=20.0)
    now = 16.0
    trim.beat()

    assert group.stale_for() == 6.0
    assert not group.is_alive()
    assert set(group.snapshot()) == {"dispatch", "trim"}
    assert set(group.snapshot()["dispatch"]) == {
        "name",
        "stale_for",
        "last_success_at",
        "last_error",
        "wedged",
    }
    assert dispatch.is_alive() is False
    assert trim.is_alive() is True


@pytest.mark.asyncio
async def test_healthz_group_exposes_loops_and_wedged_loop_is_not_masked() -> None:
    """The audit regression: a beating sibling cannot keep a wedged loop's healthz at 200."""
    port = _find_free_port()
    group = health.LivenessGroup()
    dispatch = group.register("dispatch", timeout_s=60.0)
    trim = group.register("trim", timeout_s=60.0)
    dispatch.fail("dispatch exceeded hard deadline")
    trim.beat()
    server = await health.start_health_server(
        "events_maintenance", port=port, liveness=group, image=unknown_image()
    )
    try:
        status, body = await _http_get(port, "/healthz")
        payload = json.loads(body)
        assert status == 503
        assert set(payload["loops"]) == {"dispatch", "trim"}
        assert payload["loops"]["dispatch"]["wedged"] is True
        assert payload["loops"]["trim"]["wedged"] is False
    finally:
        await health.stop_health_server(server)


@pytest.mark.asyncio
async def test_healthz_503_when_liveness_stale() -> None:
    """A stale main loop must flip /healthz to 503 so the watchdog respawns it.
    timeout_s=-1.0 keeps stale_for() (always >= 0) permanently over the bound."""
    port = _find_free_port()
    stale = health.Liveness(timeout_s=-1.0)
    server = await health.start_health_server(
        "agent_host", port=port, liveness=stale, image=unknown_image()
    )
    try:
        status, body = await _http_get(port, "/healthz")
        assert status == 503
        payload = json.loads(body)
        assert payload["name"] == "agent_host"
        assert "stale_for" in payload
        assert payload["liveness"] == "stale"
        assert payload["components"][0]["status"] == "stale"
        assert payload["degraded_reasons"] == ["loop: stale"]
    finally:
        await health.stop_health_server(server)


@pytest.mark.asyncio
async def test_healthz_200_when_liveness_fresh() -> None:
    """A beating loop keeps /healthz at 200, with stale_for reported."""
    port = _find_free_port()
    fresh = health.Liveness(timeout_s=1e9)
    server = await health.start_health_server(
        "agent_host", port=port, liveness=fresh, image=unknown_image()
    )
    try:
        status, body = await _http_get(port, "/healthz")
        assert status == 200
        assert "stale_for" in json.loads(body)
    finally:
        await health.stop_health_server(server)


@pytest.mark.asyncio
async def test_healthz_component_failure_surfaces_the_component_reason() -> None:
    port = _find_free_port()
    server = await health.start_health_server(
        "agent_host",
        port=port,
        components=[component("worker", DEGRADED, detail="job stuck")],
        image=unknown_image(),
    )
    try:
        status, body = await _http_get(port, "/healthz")
        assert status == 503
        payload = json.loads(body)
        assert payload["status"] == "degraded"
        assert payload["readiness"] == "degraded"
        assert payload["degraded_reasons"] == ["worker: job stuck"]
    finally:
        await health.stop_health_server(server)


@pytest.mark.asyncio
async def test_healthz_evaluates_component_provider_for_each_request() -> None:
    calls = 0

    def components() -> list[dict[str, object]]:
        nonlocal calls
        calls += 1
        return [component("worker", OK, progress=f"run {calls}")]

    port = _find_free_port()
    server = await health.start_health_server(
        "agent_host",
        port=port,
        components=components,
        extra=lambda: {"saturation": calls},
        image=unknown_image(),
    )
    try:
        _first_status, first = await _http_get(port, "/healthz")
        _second_status, second = await _http_get(port, "/healthz")
        assert json.loads(first)["components"][0]["progress"] == "run 1"
        assert json.loads(second)["components"][0]["progress"] == "run 2"
        assert json.loads(second)["saturation"] == 2
    finally:
        await health.stop_health_server(server)


@pytest.mark.asyncio
async def test_unknown_path_returns_404() -> None:
    port = _find_free_port()
    server = await health.start_health_server("labeler", port=port, image=unknown_image())
    try:
        status, _ = await _http_get(port, "/garbage")
        assert status == 404
    finally:
        await health.stop_health_server(server)


async def _http_request(
    port: int, method: str, path: str, *, auth: str | None = None, body: bytes = b""
) -> tuple[int, bytes]:
    """HTTP/1.1 request with optional Authorization header + body."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    head = f"{method} {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n"
    if auth is not None:
        head += f"Authorization: {auth}\r\n"
    head += f"Content-Length: {len(body)}\r\n\r\n"
    writer.write(head.encode() + body)
    await writer.drain()
    raw = await reader.read()
    writer.close()
    await writer.wait_closed()
    resp_head, _, resp_body = raw.partition(b"\r\n\r\n")
    status = int(resp_head.split(b"\r\n", 1)[0].split(b" ")[1])
    return status, resp_body


async def _ok_route(_body: bytes) -> tuple[int, bytes, str]:
    return 200, b'{"ok": true}', "application/json"


@pytest.mark.asyncio
async def test_extra_route_requires_auth_when_token_set() -> None:
    """auth_digests set: a missing / wrong bearer gets 401; a listed digest's token passes."""
    port = _find_free_port()
    server = await health.start_health_server(
        "ops",
        port=port,
        extra_routes={("POST", "/ops"): _ok_route},
        auth_digests=frozenset({token_digest("s3cret")}),
        image=unknown_image(),
    )
    try:
        s_none, _ = await _http_request(port, "POST", "/ops", body=b"{}")
        assert s_none == 401
        s_wrong, _ = await _http_request(port, "POST", "/ops", auth="Bearer nope", body=b"{}")
        assert s_wrong == 401
        s_ok, body = await _http_request(port, "POST", "/ops", auth="Bearer s3cret", body=b"{}")
        assert s_ok == 200
        assert json.loads(body)["ok"] is True
    finally:
        await health.stop_health_server(server)


@pytest.mark.asyncio
async def test_healthz_unauthenticated_even_with_auth_token() -> None:
    """/healthz stays open even when extra routes require a token — the watchdog
    probes it locally and it leaks no secret."""
    port = _find_free_port()
    server = await health.start_health_server(
        "ops",
        port=port,
        extra_routes={("POST", "/ops"): _ok_route},
        auth_digests=frozenset({token_digest("s3cret")}),
        image=unknown_image(),
    )
    try:
        status, _ = await _http_get(port, "/healthz")  # no Authorization header
        assert status == 200
    finally:
        await health.stop_health_server(server)


@pytest.mark.asyncio
async def test_extra_route_open_when_no_auth_token() -> None:
    """No auth_digests (the loopback daemons): extra routes need no bearer."""
    port = _find_free_port()
    server = await health.start_health_server(
        "memory_indexer",
        port=port,
        extra_routes={("POST", "/ops"): _ok_route},
        image=unknown_image(),
    )
    try:
        status, _ = await _http_request(port, "POST", "/ops", body=b"{}")
        assert status == 200
    finally:
        await health.stop_health_server(server)


@pytest.mark.asyncio
async def test_stop_idempotent() -> None:
    port = _find_free_port()
    server = await health.start_health_server("labeler", port=port, image=unknown_image())
    await health.stop_health_server(server)
    # Second stop does not raise
    await health.stop_health_server(server)


def test_health_port_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """With nothing overriding it, _health_port falls back to DEFAULT_PORTS.

    The overrides have to be cleared to see that: the suite pins a free port per
    daemon for the whole session (tests/fixtures/env_bootstrap.py) precisely so no test can bind
    or probe a prod default. Stubbing the settings lookup is what "unconfigured"
    means to `_health_port`."""
    monkeypatch.setattr(health, "get_field", lambda _name: None)  # pyright: ignore[reportUnknownArgumentType]
    assert health._health_port("agent_host") == 8114
    assert health._health_port("labeler") == 8103
    assert health._health_port("memory_indexer") == 8105
    assert health._health_port("heartbeat") == 8107
    assert health._health_port("task_maintenance") == 8108
    assert health._health_port("events_maintenance") == 8109


def test_health_ports_are_isolated_from_prod_defaults() -> None:
    """The session's own pinned ports are in force — the property that keeps a
    daemon leaked out of a test run off prod's ports."""
    for name in health.DEFAULT_PORTS:
        assert health._health_port(name) != health.DEFAULT_PORTS[name], (
            f"{name} health port is not isolated from its prod default"
        )
