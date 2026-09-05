"""`shared.daemon_health` — minimal asyncio HTTP server.

Validates:
- GET /healthz → 200 + JSON includes name / pid / home / started_at
- Other paths → 404
- start server + stop pair is idempotent (stop called twice does not raise)
- health_port: env override takes priority, defaults to DEFAULT_PORTS, unregistered raises KeyError
- probe_daemon: 200 only counts as alive when the name/home/pid triple matches
- probe_home: the pid-less sibling for `/api/health`, where the reload fork makes
  a pid comparison meaningless — 200 plus this unit's `$AVA_HOME`
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import cast

import pytest

from shared import daemon_health
from shared.health_schema import DEGRADED, OK, component
from shared.paths import ava_home


def _find_free_port() -> int:
    """Grabs a free localhost port — OS-assigned to avoid colliding with prod default ports."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def _http_get(port: int, path: str) -> tuple[int, bytes]:
    """Simple HTTP 1.1 GET — avoids pulling in httpx/aiohttp test dependency."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode())
    await writer.drain()
    raw = await reader.read()
    writer.close()
    await writer.wait_closed()
    head, _, body = raw.partition(b"\r\n\r\n")
    status_line = head.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
    status = int(status_line.split(" ")[1])
    return status, body


@pytest.mark.asyncio
async def test_healthz_returns_200_with_json_body() -> None:
    port = _find_free_port()
    server = await daemon_health.start_health_server("restarter", port=port)
    try:
        status, body = await _http_get(port, "/healthz")
        assert status == 200
        payload = json.loads(body)
        assert payload["name"] == "restarter"
        assert isinstance(payload["pid"], int)
        assert isinstance(payload["started_at"], float)
        assert payload["status"] == "ok"
        assert payload["readiness"] == "ok"
        assert payload["components"] == [{"name": "loop", "status": "ok"}]
        assert payload["degraded_reasons"] == []
    finally:
        await daemon_health.stop_health_server(server)


@pytest.mark.asyncio
async def test_liveness_flips_stale_then_fresh_on_beat() -> None:
    """Liveness: fresh at construction; goes stale once timeout elapses with no
    beat; beat() resets it to fresh."""
    lv = daemon_health.Liveness(timeout_s=0.05)
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
    monkeypatch.setattr(daemon_health.time, "monotonic", lambda: now)
    progress = daemon_health.LoopProgress("dispatch", timeout_s=5.0)

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
    progress = daemon_health.LoopProgress("resolution", timeout_s=60.0)
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
    monkeypatch.setattr(daemon_health.time, "monotonic", lambda: now)
    group = daemon_health.LivenessGroup()
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
    group = daemon_health.LivenessGroup()
    dispatch = group.register("dispatch", timeout_s=60.0)
    trim = group.register("trim", timeout_s=60.0)
    dispatch.fail("dispatch exceeded hard deadline")
    trim.beat()
    server = await daemon_health.start_health_server(
        "events_maintenance", port=port, liveness=group
    )
    try:
        status, body = await _http_get(port, "/healthz")
        payload = json.loads(body)
        assert status == 503
        assert set(payload["loops"]) == {"dispatch", "trim"}
        assert payload["loops"]["dispatch"]["wedged"] is True
        assert payload["loops"]["trim"]["wedged"] is False
    finally:
        await daemon_health.stop_health_server(server)


@pytest.mark.asyncio
async def test_healthz_503_when_liveness_stale() -> None:
    """A stale main loop must flip /healthz to 503 so the watchdog respawns it.
    timeout_s=-1.0 keeps stale_for() (always >= 0) permanently over the bound."""
    port = _find_free_port()
    stale = daemon_health.Liveness(timeout_s=-1.0)
    server = await daemon_health.start_health_server("restarter", port=port, liveness=stale)
    try:
        status, body = await _http_get(port, "/healthz")
        assert status == 503
        payload = json.loads(body)
        assert payload["name"] == "restarter"
        assert "stale_for" in payload
        assert payload["liveness"] == "stale"
        assert payload["components"][0]["status"] == "stale"
        assert payload["degraded_reasons"] == ["loop: stale"]
    finally:
        await daemon_health.stop_health_server(server)


@pytest.mark.asyncio
async def test_healthz_200_when_liveness_fresh() -> None:
    """A beating loop keeps /healthz at 200, with stale_for reported."""
    port = _find_free_port()
    fresh = daemon_health.Liveness(timeout_s=1e9)
    server = await daemon_health.start_health_server("restarter", port=port, liveness=fresh)
    try:
        status, body = await _http_get(port, "/healthz")
        assert status == 200
        assert "stale_for" in json.loads(body)
    finally:
        await daemon_health.stop_health_server(server)


@pytest.mark.asyncio
async def test_healthz_component_failure_surfaces_the_component_reason() -> None:
    port = _find_free_port()
    server = await daemon_health.start_health_server(
        "restarter",
        port=port,
        components=[component("worker", DEGRADED, detail="job stuck")],
    )
    try:
        status, body = await _http_get(port, "/healthz")
        assert status == 503
        payload = json.loads(body)
        assert payload["status"] == "degraded"
        assert payload["readiness"] == "degraded"
        assert payload["degraded_reasons"] == ["worker: job stuck"]
    finally:
        await daemon_health.stop_health_server(server)


@pytest.mark.asyncio
async def test_healthz_evaluates_component_provider_for_each_request() -> None:
    calls = 0

    def components() -> list[dict[str, object]]:
        nonlocal calls
        calls += 1
        return [component("worker", OK, progress=f"run {calls}")]

    port = _find_free_port()
    server = await daemon_health.start_health_server(
        "restarter",
        port=port,
        components=components,
        extra=lambda: {"saturation": calls},
    )
    try:
        _first_status, first = await _http_get(port, "/healthz")
        _second_status, second = await _http_get(port, "/healthz")
        assert json.loads(first)["components"][0]["progress"] == "run 1"
        assert json.loads(second)["components"][0]["progress"] == "run 2"
        assert json.loads(second)["saturation"] == 2
    finally:
        await daemon_health.stop_health_server(server)


@pytest.mark.asyncio
async def test_unknown_path_returns_404() -> None:
    port = _find_free_port()
    server = await daemon_health.start_health_server("labeler", port=port)
    try:
        status, _ = await _http_get(port, "/garbage")
        assert status == 404
    finally:
        await daemon_health.stop_health_server(server)


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
    """auth_token set: an extra route rejects a missing / wrong bearer with 401,
    accepts the matching one."""
    port = _find_free_port()
    server = await daemon_health.start_health_server(
        "ops",
        port=port,
        extra_routes={("POST", "/ops"): _ok_route},
        auth_token="s3cret",  # noqa: S106 — test fixture, not a real secret
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
        await daemon_health.stop_health_server(server)


@pytest.mark.asyncio
async def test_healthz_unauthenticated_even_with_auth_token() -> None:
    """/healthz stays open even when extra routes require a token — the watchdog
    probes it locally and it leaks no secret."""
    port = _find_free_port()
    server = await daemon_health.start_health_server(
        "ops",
        port=port,
        extra_routes={("POST", "/ops"): _ok_route},
        auth_token="s3cret",  # noqa: S106 — test fixture, not a real secret
    )
    try:
        status, _ = await _http_get(port, "/healthz")  # no Authorization header
        assert status == 200
    finally:
        await daemon_health.stop_health_server(server)


@pytest.mark.asyncio
async def test_extra_route_open_when_no_auth_token() -> None:
    """No auth_token (the loopback daemons): extra routes need no bearer."""
    port = _find_free_port()
    server = await daemon_health.start_health_server(
        "memory_indexer", port=port, extra_routes={("POST", "/ops"): _ok_route}
    )
    try:
        status, _ = await _http_request(port, "POST", "/ops", body=b"{}")
        assert status == 200
    finally:
        await daemon_health.stop_health_server(server)


@pytest.mark.asyncio
async def test_stop_idempotent() -> None:
    port = _find_free_port()
    server = await daemon_health.start_health_server("labeler", port=port)
    await daemon_health.stop_health_server(server)
    # Second stop does not raise
    await daemon_health.stop_health_server(server)


def test_health_port_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """With nothing overriding it, health_port falls back to DEFAULT_PORTS.

    The overrides have to be cleared to see that: the suite pins a free port per
    daemon for the whole session (tests/conftest.py) precisely so no test can bind
    or probe a prod default. Stubbing the settings lookup is what "unconfigured"
    means to `health_port`."""
    monkeypatch.setattr(daemon_health, "get_field", lambda _name: None)  # pyright: ignore[reportUnknownArgumentType]
    assert daemon_health.health_port("restarter") == 8102
    assert daemon_health.health_port("labeler") == 8103
    assert daemon_health.health_port("memory_indexer") == 8105
    assert daemon_health.health_port("heartbeat") == 8107
    assert daemon_health.health_port("task_maintenance") == 8108
    assert daemon_health.health_port("events_maintenance") == 8109


def test_health_port_fallback_warns_once_per_daemon(
    monkeypatch: pytest.MonkeyPatch,
    caplog,
) -> None:
    """F-s4-12: a unit that never declared a per-unit block falls back to the
    shared 8102-8111 segment — the fallback must be LOUD (a warning naming the
    fix), once per daemon per process, not silent and not per-call (healthchecks
    call health_port every round)."""
    monkeypatch.setattr(daemon_health, "get_field", lambda _name: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(daemon_health, "_warned_shared_default", set())  # pyright: ignore[reportUnknownArgumentType]
    with caplog.at_level(logging.WARNING, logger="shared.daemon_health"):  # pyright: ignore[reportUnknownMemberType]
        assert daemon_health.health_port("im_bridge") == 8111
        assert daemon_health.health_port("im_bridge") == 8111  # same daemon: silent now
        assert daemon_health.health_port("delivery_watchdog") == 8110  # new daemon: warns
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]  # pyright: ignore[reportUnknownMemberType]
    assert len(warnings) == 2  # pyright: ignore[reportUnknownArgumentType]
    assert "im_bridge" in warnings[0].getMessage()  # pyright: ignore[reportUnknownMemberType]
    assert "--health-port-base" in warnings[0].getMessage()  # pyright: ignore[reportUnknownMemberType]


def test_health_ports_are_isolated_from_prod_defaults() -> None:
    """The session's own pinned ports are in force — the property that keeps a
    daemon leaked out of a test run off prod's ports."""
    for name in daemon_health.DEFAULT_PORTS:
        assert daemon_health.health_port(name) != daemon_health.DEFAULT_PORTS[name], (
            f"{name} health port is not isolated from its prod default"
        )


# ─── probe_daemon: a 200 is believed only from a verified identity ───────────
#
# Regression cover for the 2026-07-24 outage: a pytest-leaked restarter daemon
# fell back to prod's default 8102 and answered 200 for 98 minutes while prod's
# own restarter was dead. Every probe read green, so the watchdog never
# respawned. These run a REAL health server on a free port and vary exactly one
# element of the identity at a time.


def _probe_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/healthz"


async def _probe(name: str, port: int, pidfile: Path, **kw: object) -> daemon_health.DaemonProbe:
    """Run the (blocking) probe off the event loop.

    `probe_daemon` is sync by design — its callers are cron-invoked healthchecks
    and the watchdog, which already runs each check via `asyncio.to_thread`.
    Calling it inline here would block the same loop that serves the health
    server under test, and every probe would "time out" against a live daemon."""
    return await asyncio.to_thread(
        daemon_health.probe_daemon,
        name,
        _probe_url(port),
        pidfile=pidfile,
        **kw,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_healthz_body_carries_home(tmp_path: Path) -> None:
    """`home` is in the payload at all — the field the cross-cluster check reads."""
    port = _find_free_port()
    server = await daemon_health.start_health_server("restarter", port=port)
    try:
        _status, body = await _http_get(port, "/healthz")
        assert json.loads(body)["home"] == str(ava_home())
    finally:
        await daemon_health.stop_health_server(server)


@pytest.mark.asyncio
async def test_healthz_body_carries_the_daemons_own_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    """`sha` is answered by the daemon process itself, so a plain curl tells a
    daemon still holding pre-rollout code from one that restarted onto it — the
    per-daemon view the machine-level roster row cannot give (it speaks only for
    whichever process answers the status probe)."""
    monkeypatch.setattr(daemon_health.process_sha, "get", lambda: "c0ffee1234")
    port = _find_free_port()
    server = await daemon_health.start_health_server("restarter", port=port)
    try:
        _status, body = await _http_get(port, "/healthz")
        assert json.loads(body)["sha"] == "c0ffee1234"
    finally:
        await daemon_health.stop_health_server(server)


@pytest.mark.asyncio
async def test_healthz_reports_an_unfrozen_process_as_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A daemon that froze no commit says so rather than omitting the key — an
    absent field reads to a probe as an old daemon that predates this payload,
    a null reads as "this process cannot vouch for its code"."""
    monkeypatch.setattr(daemon_health.process_sha, "get", lambda: None)
    port = _find_free_port()
    server = await daemon_health.start_health_server("restarter", port=port)
    try:
        _status, body = await _http_get(port, "/healthz")
        assert json.loads(body)["sha"] is None
    finally:
        await daemon_health.stop_health_server(server)


@pytest.mark.asyncio
async def test_probe_reports_the_commit_without_judging_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The commit rides in the detail, never in the verdict.

    A daemon on stale code is alive. Failing the probe on a commit mismatch
    would have every watchdog respawn its daemon the moment a rollout advances
    the checkout, racing the orchestrated restart it is supposed to leave alone."""
    monkeypatch.setattr(daemon_health.process_sha, "get", lambda: "c0ffee1234")
    port = _find_free_port()
    pidfile = tmp_path / "restarter.pid"
    pidfile.write_text(str(os.getpid()))
    server = await daemon_health.start_health_server("restarter", port=port)
    try:
        probe = await _probe("restarter", port, pidfile)
        assert probe.alive is True, probe.detail
        assert "c0ffee1" in probe.detail
    finally:
        await daemon_health.stop_health_server(server)


@pytest.mark.asyncio
async def test_probe_alive_when_name_home_and_pid_all_match(tmp_path: Path) -> None:
    """The happy path: our own daemon, its own pidfile → alive."""
    port = _find_free_port()
    pidfile = tmp_path / "restarter.pid"
    pidfile.write_text(str(os.getpid()))
    server = await daemon_health.start_health_server("restarter", port=port)
    try:
        probe = await _probe("restarter", port, pidfile)
        assert probe.alive is True, probe.detail
    finally:
        await daemon_health.stop_health_server(server)


@pytest.mark.asyncio
async def test_probe_rejects_200_from_a_process_that_is_not_ours(tmp_path: Path) -> None:
    """THE outage: something answers a healthy 200 on our port, but the pid is not
    the one our pidfile recorded → dead, so the watchdog respawns instead of
    idling on a stranger's green light.

    Verdict DOWN, not terminal: name and home already matched, so the stray belongs
    to THIS cluster and this daemon kind — `respawn_service` kills our own
    `ava-restarter` session first, which does free the port."""
    port = _find_free_port()
    pidfile = tmp_path / "restarter.pid"
    pidfile.write_text(str(os.getpid() + 1))  # our daemon's pid, not the responder's
    server = await daemon_health.start_health_server("restarter", port=port)
    try:
        probe = await _probe("restarter", port, pidfile)
        assert probe.verdict is daemon_health.ProbeVerdict.DOWN
        assert probe.terminal is False
        assert "pid" in probe.detail
    finally:
        await daemon_health.stop_health_server(server)


@pytest.mark.asyncio
async def test_probe_rejects_daemon_from_another_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A daemon of another UNIT (different `$AVA_HOME`) holding our port is not
    our daemon, even with a matching name and a pid we would accept.

    Verdict PORT_TAKEN — terminal. This is the `win`/WSL2 case: the foreign daemon
    runs under its own home's socket, so no respawn this unit can
    perform frees the port, and the 2026-07-29 loop respawned into that wall every
    60s for hours. The two units there were in the SAME cluster (#977), which is
    why the detail must say unit — and why the sentence is asserted here, on the
    string the probe really emits, rather than only where a stub fabricates it."""
    port = _find_free_port()
    pidfile = tmp_path / "restarter.pid"
    pidfile.write_text(str(os.getpid()))
    server = await daemon_health.start_health_server("restarter", port=port)
    try:
        # The server answered with the real home; make the PROBE side believe it
        # belongs to a different unit — the same asymmetry a foreign daemon has.
        monkeypatch.setattr(daemon_health, "ava_home", lambda: tmp_path / "other-home")
        probe = await _probe("restarter", port, pidfile)
        assert probe.verdict is daemon_health.ProbeVerdict.PORT_TAKEN
        assert probe.terminal is True
        assert "home=" in probe.detail
        assert "another unit's daemon holds this port" in probe.detail
    finally:
        await daemon_health.stop_health_server(server)


@pytest.mark.asyncio
async def test_probe_rejects_a_different_daemon_kind(tmp_path: Path) -> None:
    """A labeler squatting on the restarter's port is not a live restarter — and
    terminal, because killing `ava-restarter` does not free a port `ava-labeler`
    holds."""
    port = _find_free_port()
    pidfile = tmp_path / "restarter.pid"
    pidfile.write_text(str(os.getpid()))
    server = await daemon_health.start_health_server("labeler", port=port)
    try:
        probe = await _probe("restarter", port, pidfile)
        assert probe.verdict is daemon_health.ProbeVerdict.PORT_TAKEN
        assert "name=" in probe.detail
    finally:
        await daemon_health.stop_health_server(server)


@pytest.mark.asyncio
async def test_probe_rejects_200_when_no_pidfile_exists(tmp_path: Path) -> None:
    """No pidfile + something answering = an impostor, NOT "unverifiable, assume
    alive". Daemons write their pidfile before binding /healthz, so our own
    daemon can never be in this state.

    DOWN rather than terminal: name and home matched first, so the answerer is a
    stray of this same cluster, which the respawn's kill-session clears."""
    port = _find_free_port()
    server = await daemon_health.start_health_server("restarter", port=port)
    try:
        probe = await _probe("restarter", port, tmp_path / "absent.pid")
        assert probe.verdict is daemon_health.ProbeVerdict.DOWN
        assert "pidfile" in probe.detail
    finally:
        await daemon_health.stop_health_server(server)


@pytest.mark.asyncio
async def test_probe_rejects_non_json_responder(tmp_path: Path) -> None:
    """An unrelated HTTP server on the port (no JSON identity) → dead, and
    terminal: it is not an Ava daemon at all, so nothing this unit supervises can
    be restarted to take the port back."""

    async def _handle(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _reader.read(1024)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\nhello")
        await writer.drain()
        writer.close()

    port = _find_free_port()
    server = await asyncio.start_server(_handle, host="127.0.0.1", port=port)
    pidfile = tmp_path / "restarter.pid"
    pidfile.write_text(str(os.getpid()))
    try:
        probe = await _probe("restarter", port, pidfile)
        assert probe.verdict is daemon_health.ProbeVerdict.PORT_TAKEN
        assert "not JSON" in probe.detail
    finally:
        await daemon_health.stop_health_server(server)


def test_probe_dead_when_nothing_listens(tmp_path: Path) -> None:
    pidfile = tmp_path / "restarter.pid"
    pidfile.write_text(str(os.getpid()))
    probe = daemon_health.probe_daemon(
        "restarter", _probe_url(_find_free_port()), pidfile=pidfile, timeout_s=1.0
    )
    assert probe.verdict is daemon_health.ProbeVerdict.DOWN
    assert probe.terminal is False, "a free port is the respawnable case"
    assert "unreachable" in probe.detail


@pytest.mark.asyncio
async def test_probe_dead_when_liveness_is_stale(tmp_path: Path) -> None:
    """A wedged main loop flips /healthz to 503; identity is irrelevant then.

    DOWN, so the respawn still runs — a wedged loop in our own daemon is exactly
    what a respawn cures."""
    port = _find_free_port()
    pidfile = tmp_path / "restarter.pid"
    pidfile.write_text(str(os.getpid()))
    stale = daemon_health.Liveness(timeout_s=-1.0)
    server = await daemon_health.start_health_server("restarter", port=port, liveness=stale)
    try:
        probe = await _probe("restarter", port, pidfile)
        assert probe.verdict is daemon_health.ProbeVerdict.DOWN
    finally:
        await daemon_health.stop_health_server(server)


@pytest.mark.asyncio
async def test_probe_includes_degraded_component_reasons(tmp_path: Path) -> None:
    """A watchdog failure names the stuck component rather than an opaque 503."""
    port = _find_free_port()
    pidfile = tmp_path / "restarter.pid"
    pidfile.write_text(str(os.getpid()))
    server = await daemon_health.start_health_server(
        "restarter",
        port=port,
        components=[component("ops", DEGRADED, detail="update-lock held 7200s")],
    )
    try:
        probe = await _probe("restarter", port, pidfile)
        assert probe.verdict is daemon_health.ProbeVerdict.DOWN
        assert probe.detail == "healthz returned HTTP 503; degraded: ops: update-lock held 7200s"
    finally:
        await daemon_health.stop_health_server(server)


def test_health_port_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    # Settings is a one-shot BaseSettings loaded at module import; monkeypatch.setenv
    # cannot change the already-imported settings.services.restarter_health_port — use
    # monkeypatch.setattr directly on the Settings instance field, consistent with other
    # tests that migrated to Settings (see tests/ava/test_web.py, test_vision.py for the same pattern).
    from shared.config import settings

    monkeypatch.setattr(settings.services, "restarter_health_port", 9999)
    assert daemon_health.health_port("restarter") == 9999


def test_health_port_unknown_raises_key_error() -> None:
    """Unregistered daemon name — fail fast (no silent fallback)."""
    with pytest.raises(KeyError):
        daemon_health.health_port("never_registered_daemon")


# ─── probe_daemon always returns a verdict ───────────────────────────────
#
# The watchdog isolates each check, so a raising probe never took a round down —
# it just meant the service was never judged alive-or-dead, so NO RESTART was
# ever attempted, while every 60s round wrote a fresh multi-KB traceback. Six
# healthchecks route through probe_daemon (heartbeat, labeler, memory-indexer,
# ops, events-maintenance, restarter), so one escaping exception type silences
# six services' revival at once.


def test_probe_daemon_survives_an_http_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`http.client.HTTPException` is NOT an OSError, so the inner probe's narrow
    catch misses it — a malformed status line or truncated body from whatever
    holds the port reaches the wrapper."""
    import http.client

    def _boom(*_a: object, **_k: object) -> None:
        raise http.client.BadStatusLine("garbage on the wire")

    monkeypatch.setattr(daemon_health.urllib.request, "urlopen", _boom)
    pidfile = tmp_path / "restarter.pid"
    pidfile.write_text(str(os.getpid()))
    with caplog.at_level(logging.ERROR, logger="shared.daemon_health"):
        probe = daemon_health.probe_daemon("restarter", _probe_url(9), pidfile=pidfile)
    assert probe.alive is False
    assert "BadStatusLine" in probe.detail
    assert any("raised unexpectedly" in r.getMessage() for r in caplog.records)


def test_probe_daemon_survives_a_pidfile_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_recorded_pid` catches FileNotFoundError/ValueError but runs OUTSIDE the
    inner probe's try — a PermissionError or IsADirectoryError on the pidfile
    would have escaped."""
    monkeypatch.setattr(
        daemon_health,
        "_probe_daemon",
        lambda *_a, **_k: (_ for _ in ()).throw(PermissionError("pidfile unreadable")),  # pyright: ignore[reportUnknownArgumentType]
    )
    probe = daemon_health.probe_daemon("restarter", _probe_url(9), pidfile=tmp_path / "x.pid")
    assert probe.alive is False
    assert "PermissionError" in probe.detail


def test_probe_daemon_verdict_is_down_never_up_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wrapper must fail CLOSED. Reporting alive on an unreadable probe would
    make the watchdog skip a genuinely dead daemon forever.

    It reports DOWN, never terminal: an unforeseen probe failure is not evidence
    that a foreign process holds the port, and calling it terminal would stop the
    revival of a daemon a respawn could have saved."""
    monkeypatch.setattr(
        daemon_health,
        "_probe_daemon",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("nobody predicted this")),  # pyright: ignore[reportUnknownArgumentType]
    )
    probe = daemon_health.probe_daemon("labeler", _probe_url(9), pidfile=tmp_path / "x.pid")
    assert probe.verdict is daemon_health.ProbeVerdict.DOWN
    assert probe.terminal is False


def test_probe_daemon_passes_through_a_normal_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wrapper is transparent when the probe answers — it adds a floor, not a
    behaviour change."""
    monkeypatch.setattr(
        daemon_health,
        "_probe_daemon",
        lambda *_a, **_k: daemon_health.DaemonProbe.up("pid 42"),  # pyright: ignore[reportUnknownArgumentType]
    )
    probe = daemon_health.probe_daemon("ops", _probe_url(9), pidfile=tmp_path / "x.pid")
    assert probe.alive is True
    assert probe.detail == "pid 42"


# ─── probe_home: identity without a pid ──────────────────────────────────
#
# The gateway serves `/api/health`, which `probe_home` checks on `home` alone —
# no `pid`, because uvicorn's reload fork means a healthy gateway routinely
# answers with a pid its own pidfile never recorded, and not yet `name`, which
# the payload now carries but no probe may read until it has rolled out
# fleet-wide (#1038). `probe_home` is that weaker check, shared by the gateway
# healthcheck and (through `ServiceSpec.identity_probe`) by `ava status` and
# `ava cluster health-probe`, so the watchdog and the operator cannot be told
# different things about the same port.


class _Resp:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self, _n: int = -1) -> bytes:
        return self._body

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *_a: object) -> None:
        return None


def _answer(monkeypatch: pytest.MonkeyPatch, status: int, body: bytes) -> None:
    monkeypatch.setattr(urllib.request, "urlopen", lambda _url, **_kw: _Resp(status, body))  # pyright: ignore[reportUnknownArgumentType]


def test_probe_home_alive_when_home_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Also the rollout-tolerance pin: this body is what a gateway that predates
    the `name` field answers with, and during a rolling upgrade a runner on new
    code probes exactly such a gateway. A `name` mismatch is PORT_TAKEN, which is
    terminal, so the moment this stops reading ALIVE every not-yet-updated
    gateway in the fleet is terminal-failed by its own watchdog."""
    _answer(monkeypatch, 200, json.dumps({"status": "ok", "home": str(ava_home())}).encode())
    probe = daemon_health.probe_home("http://127.0.0.1:9/api/health")
    assert probe.verdict is daemon_health.ProbeVerdict.ALIVE


def test_probe_home_alive_on_a_payload_carrying_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half of the same window: an updated gateway's body, read by a
    probe that does not yet know the field. The extra key must be inert — the
    expand half of expand-contract is only safe if old readers ignore it."""
    _answer(
        monkeypatch,
        200,
        json.dumps({"status": "ok", "name": "gateway", "home": str(ava_home())}).encode(),
    )
    probe = daemon_health.probe_home("http://127.0.0.1:9/api/health")
    assert probe.verdict is daemon_health.ProbeVerdict.ALIVE


def test_probe_home_rejects_another_units_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """The impostor case, and it is TERMINAL: this unit cannot kill a session on
    another UNIT's socket — the socket lives under `$AVA_HOME`, so a foreign
    unit is exactly as unkillable as a foreign cluster — and respawning into the
    bound port is a loop.

    The emitted sentence is pinned here, not just the home it names: `home` is the
    unit identity, and calling it a cluster is what sent #977's first diagnosis
    hunting an allocation bug that did not exist."""
    _answer(monkeypatch, 200, json.dumps({"home": "/home/ava/.ava"}).encode())
    probe = daemon_health.probe_home("http://127.0.0.1:9/api/health")
    assert probe.verdict is daemon_health.ProbeVerdict.PORT_TAKEN
    assert "/home/ava/.ava" in probe.detail
    assert "another unit's daemon holds this port" in probe.detail


def test_probe_home_rejects_a_body_with_no_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 200 that says nothing about who answered is not evidence it is ours."""
    _answer(monkeypatch, 200, b'{"status": "ok"}')
    assert daemon_health.probe_home("http://127.0.0.1:9/api/health").alive is False


def test_probe_home_unreachable_is_down_not_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing on the port is the respawnable case, exactly as for probe_daemon."""

    def _refuse(*_a: object, **_k: object) -> _Resp:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", _refuse)
    probe = daemon_health.probe_home("http://127.0.0.1:9/api/health")
    assert probe.verdict is daemon_health.ProbeVerdict.DOWN
    assert probe.terminal is False


def test_probe_home_always_returns_a_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail CLOSED — an unreadable probe is reported down, never alive."""
    monkeypatch.setattr(
        daemon_health,
        "_probe_home",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("nobody predicted this")),  # pyright: ignore[reportUnknownArgumentType]
    )
    probe = daemon_health.probe_home("http://127.0.0.1:9/api/health")
    assert probe.verdict is daemon_health.ProbeVerdict.DOWN
    assert "RuntimeError" in probe.detail


def test_health_port_warns_once_on_windows_8106(
    monkeypatch: pytest.MonkeyPatch,
    caplog,
) -> None:
    """#1179: Windows iphlpsvc permanently holds 8106 — a daemon whose port
    resolves there (default OR explicit override) must be named loudly, once
    per daemon per process, so the misconfiguration is not silent."""
    import os as _os

    monkeypatch.setattr(_os, "name", "nt")
    monkeypatch.setattr(daemon_health, "_warned_windows_8106", set())  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        daemon_health,
        "get_field",
        lambda _name: 8106,  # pyright: ignore[reportUnknownArgumentType]
    )
    with caplog.at_level(logging.WARNING, logger="shared.daemon_health"):  # pyright: ignore[reportUnknownMemberType]
        assert daemon_health.health_port("events_maintenance") == 8106
        assert daemon_health.health_port("events_maintenance") == 8106  # same daemon: silent now
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]  # pyright: ignore[reportUnknownMemberType]
    assert len(warnings) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert "8106" in warnings[0].getMessage()  # pyright: ignore[reportUnknownMemberType]
    assert "iphlpsvc" in warnings[0].getMessage()  # pyright: ignore[reportUnknownMemberType]
