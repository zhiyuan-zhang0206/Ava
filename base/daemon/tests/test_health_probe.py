"""`base.daemon.health` probe paths: probe_daemon/probe_home fresh-vs-stale verdicts,
commit/identity checks, and the Windows 8106 port-warning; split from base/daemon/tests/test_health.py (task #4922)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from base.daemon import health
from base.daemon.health_schema import DEGRADED, component
from base.daemon.tests._health_helpers import _find_free_port, _http_get, _probe_url
from base.paths import ava_home

# ─── probe_daemon: a 200 is believed only from a verified identity ───────────
#
# Regression cover for the 2026-07-24 outage: a pytest-leaked daemon fell back
# to prod's default health port and answered 200 for 98 minutes while prod's
# own daemon was dead. Every probe read green, so the watchdog never
# respawned. These run a REAL health server on a free port and vary exactly one
# element of the identity at a time.


async def _probe(name: str, port: int, pidfile: Path, **kw: object) -> health.DaemonProbe:
    """Run the (blocking) probe off the event loop.

    `probe_daemon` is sync by design — its callers are cron-invoked healthchecks
    and the watchdog, which already runs each check via `asyncio.to_thread`.
    Calling it inline here would block the same loop that serves the health
    server under test, and every probe would "time out" against a live daemon."""
    return await asyncio.to_thread(
        health.probe_daemon,
        name,
        _probe_url(port),
        pidfile=pidfile,
        **kw,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_healthz_body_carries_home(tmp_path: Path) -> None:
    """`home` is in the payload at all — the field the cross-cluster check reads."""
    port = _find_free_port()
    server = await health.start_health_server("agent_host", port=port)
    try:
        _status, body = await _http_get(port, "/healthz")
        assert json.loads(body)["home"] == str(ava_home())
    finally:
        await health.stop_health_server(server)


@pytest.mark.asyncio
async def test_healthz_body_carries_the_daemons_own_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    """`sha` is answered by the daemon process itself, so a plain curl tells a
    daemon still holding pre-rollout code from one that restarted onto it — the
    per-daemon view the machine-level roster row cannot give (it speaks only for
    whichever process answers the status probe)."""
    monkeypatch.setattr(health.loaded_commit, "get", lambda: "c0ffee1234")
    port = _find_free_port()
    server = await health.start_health_server("agent_host", port=port)
    try:
        _status, body = await _http_get(port, "/healthz")
        assert json.loads(body)["sha"] == "c0ffee1234"
    finally:
        await health.stop_health_server(server)


@pytest.mark.asyncio
async def test_healthz_reports_an_unfrozen_process_as_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A daemon that froze no commit says so rather than omitting the key — an
    absent field reads to a probe as an old daemon that predates this payload,
    a null reads as "this process cannot vouch for its code"."""
    monkeypatch.setattr(health.loaded_commit, "get", lambda: None)
    port = _find_free_port()
    server = await health.start_health_server("agent_host", port=port)
    try:
        _status, body = await _http_get(port, "/healthz")
        assert json.loads(body)["sha"] is None
    finally:
        await health.stop_health_server(server)


@pytest.mark.asyncio
async def test_probe_reports_the_commit_without_judging_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The commit rides in the detail, never in the verdict.

    A daemon on stale code is alive. Failing the probe on a commit mismatch
    would have every watchdog respawn its daemon the moment a rollout advances
    the checkout, racing the orchestrated restart it is supposed to leave alone."""
    monkeypatch.setattr(health.loaded_commit, "get", lambda: "c0ffee1234")
    port = _find_free_port()
    pidfile = tmp_path / "agent_host.pid"
    pidfile.write_text(str(os.getpid()))
    server = await health.start_health_server("agent_host", port=port)
    try:
        probe = await _probe("agent_host", port, pidfile)
        assert probe.alive is True, probe.detail
        assert "c0ffee1" in probe.detail
    finally:
        await health.stop_health_server(server)


@pytest.mark.asyncio
async def test_probe_alive_when_name_home_and_pid_all_match(tmp_path: Path) -> None:
    """The happy path: our own daemon, its own pidfile → alive."""
    port = _find_free_port()
    pidfile = tmp_path / "agent_host.pid"
    pidfile.write_text(str(os.getpid()))
    server = await health.start_health_server("agent_host", port=port)
    try:
        probe = await _probe("agent_host", port, pidfile)
        assert probe.alive is True, probe.detail
    finally:
        await health.stop_health_server(server)


@pytest.mark.asyncio
async def test_probe_rejects_200_from_a_process_that_is_not_ours(tmp_path: Path) -> None:
    """THE outage: something answers a healthy 200 on our port, but the pid is not
    the one our pidfile recorded → dead, so the watchdog respawns instead of
    idling on a stranger's green light.

    Verdict DOWN, not terminal: name and home already matched, so the stray belongs
    to THIS cluster and this daemon kind — `respawn_service` kills our own
    `ava-agent-host` session first, which does free the port."""
    port = _find_free_port()
    pidfile = tmp_path / "agent_host.pid"
    pidfile.write_text(str(os.getpid() + 1))  # our daemon's pid, not the responder's
    server = await health.start_health_server("agent_host", port=port)
    try:
        probe = await _probe("agent_host", port, pidfile)
        assert probe.verdict is health.ProbeVerdict.DOWN
        assert probe.terminal is False
        assert "pid" in probe.detail
    finally:
        await health.stop_health_server(server)


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
    pidfile = tmp_path / "agent_host.pid"
    pidfile.write_text(str(os.getpid()))
    server = await health.start_health_server("agent_host", port=port)
    try:
        # The server answered with the real home; make the PROBE side believe it
        # belongs to a different unit — the same asymmetry a foreign daemon has.
        monkeypatch.setattr(health, "ava_home", lambda: tmp_path / "other-home")
        probe = await _probe("agent_host", port, pidfile)
        assert probe.verdict is health.ProbeVerdict.PORT_TAKEN
        assert probe.terminal is True
        assert "home=" in probe.detail
        assert "another unit's daemon holds this port" in probe.detail
    finally:
        await health.stop_health_server(server)


@pytest.mark.asyncio
async def test_probe_rejects_a_different_daemon_kind(tmp_path: Path) -> None:
    """A labeler squatting on the agent host's port is not a live agent host — and
    terminal, because killing `ava-agent-host` does not free a port `ava-labeler`
    holds."""
    port = _find_free_port()
    pidfile = tmp_path / "agent_host.pid"
    pidfile.write_text(str(os.getpid()))
    server = await health.start_health_server("labeler", port=port)
    try:
        probe = await _probe("agent_host", port, pidfile)
        assert probe.verdict is health.ProbeVerdict.PORT_TAKEN
        assert "name=" in probe.detail
    finally:
        await health.stop_health_server(server)


@pytest.mark.asyncio
async def test_probe_rejects_200_when_no_pidfile_exists(tmp_path: Path) -> None:
    """No pidfile + something answering = an impostor, NOT "unverifiable, assume
    alive". Daemons write their pidfile before binding /healthz, so our own
    daemon can never be in this state.

    DOWN rather than terminal: name and home matched first, so the answerer is a
    stray of this same cluster, which the respawn's kill-session clears."""
    port = _find_free_port()
    server = await health.start_health_server("agent_host", port=port)
    try:
        probe = await _probe("agent_host", port, tmp_path / "absent.pid")
        assert probe.verdict is health.ProbeVerdict.DOWN
        assert "pidfile" in probe.detail
    finally:
        await health.stop_health_server(server)


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
    pidfile = tmp_path / "agent_host.pid"
    pidfile.write_text(str(os.getpid()))
    try:
        probe = await _probe("agent_host", port, pidfile)
        assert probe.verdict is health.ProbeVerdict.PORT_TAKEN
        assert "not JSON" in probe.detail
    finally:
        await health.stop_health_server(server)


def test_probe_dead_when_nothing_listens(tmp_path: Path) -> None:
    pidfile = tmp_path / "agent_host.pid"
    pidfile.write_text(str(os.getpid()))
    probe = health.probe_daemon(
        "agent_host", _probe_url(_find_free_port()), pidfile=pidfile, timeout_s=1.0
    )
    assert probe.verdict is health.ProbeVerdict.DOWN
    assert probe.terminal is False, "a free port is the respawnable case"
    assert "unreachable" in probe.detail


@pytest.mark.asyncio
async def test_probe_dead_when_liveness_is_stale(tmp_path: Path) -> None:
    """A wedged main loop flips /healthz to 503; identity is irrelevant then.

    DOWN, so the respawn still runs — a wedged loop in our own daemon is exactly
    what a respawn cures."""
    port = _find_free_port()
    pidfile = tmp_path / "agent_host.pid"
    pidfile.write_text(str(os.getpid()))
    stale = health.Liveness(timeout_s=-1.0)
    server = await health.start_health_server("agent_host", port=port, liveness=stale)
    try:
        probe = await _probe("agent_host", port, pidfile)
        assert probe.verdict is health.ProbeVerdict.DOWN
    finally:
        await health.stop_health_server(server)


@pytest.mark.asyncio
async def test_probe_includes_degraded_component_reasons(tmp_path: Path) -> None:
    """A watchdog failure names the stuck component rather than an opaque 503."""
    port = _find_free_port()
    pidfile = tmp_path / "agent_host.pid"
    pidfile.write_text(str(os.getpid()))
    server = await health.start_health_server(
        "agent_host",
        port=port,
        components=[component("ops", DEGRADED, detail="update-lock held 7200s")],
    )
    try:
        probe = await _probe("agent_host", port, pidfile)
        assert probe.verdict is health.ProbeVerdict.DOWN
        assert probe.detail == "healthz returned HTTP 503; degraded: ops: update-lock held 7200s"
    finally:
        await health.stop_health_server(server)


def test_health_port_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    # Settings is a one-shot BaseSettings loaded at module import; monkeypatch.setenv
    # cannot change the already-imported settings.services.agent_host_health_port — use
    # monkeypatch.setattr directly on the Settings instance field, consistent with other
    # tests that migrated to Settings (see ava/tests/test_web.py, test_vision.py for the same pattern).
    from base.config import settings

    monkeypatch.setattr(settings.services, "agent_host_health_port", 9999)
    assert health._health_port("agent_host") == 9999


def test_health_port_unknown_raises_key_error() -> None:
    """Unregistered daemon name — fail fast (no silent fallback)."""
    with pytest.raises(KeyError):
        health._health_port("never_registered_daemon")


# ─── probe_daemon always returns a verdict ───────────────────────────────
#
# The watchdog isolates each check, so a raising probe never took a round down —
# it just meant the service was never judged alive-or-dead, so NO RESTART was
# ever attempted, while every 60s round wrote a fresh multi-KB traceback. Six
# healthchecks route through probe_daemon (heartbeat, labeler, memory-indexer,
# ops, events-maintenance, agent_host), so one escaping exception type silences
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

    monkeypatch.setattr(health.urllib.request, "urlopen", _boom)
    pidfile = tmp_path / "agent_host.pid"
    pidfile.write_text(str(os.getpid()))
    with caplog.at_level(logging.ERROR, logger="base.daemon.health"):
        probe = health.probe_daemon("agent_host", _probe_url(9), pidfile=pidfile)
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
        health,
        "_probe_daemon",
        lambda *_a, **_k: (_ for _ in ()).throw(PermissionError("pidfile unreadable")),  # pyright: ignore[reportUnknownArgumentType]
    )
    probe = health.probe_daemon("agent_host", _probe_url(9), pidfile=tmp_path / "x.pid")
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
        health,
        "_probe_daemon",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("nobody predicted this")),  # pyright: ignore[reportUnknownArgumentType]
    )
    probe = health.probe_daemon("labeler", _probe_url(9), pidfile=tmp_path / "x.pid")
    assert probe.verdict is health.ProbeVerdict.DOWN
    assert probe.terminal is False


def test_probe_daemon_passes_through_a_normal_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wrapper is transparent when the probe answers — it adds a floor, not a
    behaviour change."""
    monkeypatch.setattr(
        health,
        "_probe_daemon",
        lambda *_a, **_k: health.DaemonProbe.up("pid 42"),  # pyright: ignore[reportUnknownArgumentType]
    )
    probe = health.probe_daemon("ops", _probe_url(9), pidfile=tmp_path / "x.pid")
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
    probe = health.probe_home("http://127.0.0.1:9/api/health")
    assert probe.verdict is health.ProbeVerdict.ALIVE


def test_probe_home_alive_on_a_payload_carrying_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half of the same window: an updated gateway's body, read by a
    probe that does not yet know the field. The extra key must be inert — the
    expand half of expand-contract is only safe if old readers ignore it."""
    _answer(
        monkeypatch,
        200,
        json.dumps({"status": "ok", "name": "gateway", "home": str(ava_home())}).encode(),
    )
    probe = health.probe_home("http://127.0.0.1:9/api/health")
    assert probe.verdict is health.ProbeVerdict.ALIVE


def test_probe_home_rejects_another_units_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """The impostor case, and it is TERMINAL: this unit cannot kill a session on
    another UNIT's socket — the socket lives under `$AVA_HOME`, so a foreign
    unit is exactly as unkillable as a foreign cluster — and respawning into the
    bound port is a loop.

    The emitted sentence is pinned here, not just the home it names: `home` is the
    unit identity, and calling it a cluster is what sent #977's first diagnosis
    hunting an allocation bug that did not exist."""
    _answer(monkeypatch, 200, json.dumps({"home": "/home/ava/.ava"}).encode())
    probe = health.probe_home("http://127.0.0.1:9/api/health")
    assert probe.verdict is health.ProbeVerdict.PORT_TAKEN
    assert "/home/ava/.ava" in probe.detail
    assert "another unit's daemon holds this port" in probe.detail


def test_probe_home_rejects_a_body_with_no_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 200 that says nothing about who answered is not evidence it is ours."""
    _answer(monkeypatch, 200, b'{"status": "ok"}')
    assert health.probe_home("http://127.0.0.1:9/api/health").alive is False


def test_probe_home_unreachable_is_down_not_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing on the port is the respawnable case, exactly as for probe_daemon."""

    def _refuse(*_a: object, **_k: object) -> _Resp:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", _refuse)
    probe = health.probe_home("http://127.0.0.1:9/api/health")
    assert probe.verdict is health.ProbeVerdict.DOWN
    assert probe.terminal is False


def test_probe_home_always_returns_a_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail CLOSED — an unreadable probe is reported down, never alive."""
    monkeypatch.setattr(
        health,
        "_probe_home",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("nobody predicted this")),  # pyright: ignore[reportUnknownArgumentType]
    )
    probe = health.probe_home("http://127.0.0.1:9/api/health")
    assert probe.verdict is health.ProbeVerdict.DOWN
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
    monkeypatch.setattr(health, "_warned_windows_8106", set())  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        health,
        "get_field",
        lambda _name: 8106,  # pyright: ignore[reportUnknownArgumentType]
    )
    with caplog.at_level(logging.WARNING, logger="base.daemon.health"):  # pyright: ignore[reportUnknownMemberType]
        assert health._health_port("events_maintenance") == 8106
        assert health._health_port("events_maintenance") == 8106  # same daemon: silent now
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]  # pyright: ignore[reportUnknownMemberType]
    assert len(warnings) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert "8106" in warnings[0].getMessage()  # pyright: ignore[reportUnknownMemberType]
    assert "iphlpsvc" in warnings[0].getMessage()  # pyright: ignore[reportUnknownMemberType]


def test_probe_fails_closed_on_any_unexpected_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail CLOSED — reporting alive on an unreadable probe would make the
    watchdog skip a genuinely dead gateway forever."""
    monkeypatch.setattr(
        health,
        "_probe_home",
        lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("unpredicted")),  # pyright: ignore[reportUnknownArgumentType]
    )
    assert health.probe_home("http://gateway.example/api/health").alive is False


def test_probe_passes_through_a_normal_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    """The wrapper adds a floor, not a behaviour change."""
    monkeypatch.setattr(health, "_probe_home", lambda *_a, **_kw: health.DaemonProbe.up("home /x"))  # pyright: ignore[reportUnknownArgumentType]
    probe = health.probe_home("http://gateway.example/api/health")
    assert probe.alive is True
    assert probe.detail == "home /x"
