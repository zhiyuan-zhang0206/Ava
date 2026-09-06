"""Identity mismatch is a TERMINAL state, not something to respawn against.

Measured on a Windows box, 2026-07-29. A Windows unit (`C:\\Users\\ava\\.ava`) and
a WSL2 unit (`/home/ava/.ava`) share one localhost namespace, because WSL2 forwards
the Linux unit's listeners onto Windows' localhost. So the Windows healthchecks
probed `:8102` / `:8106` and reached the *other unit's* daemons.

The identity check did its job and called that dead. What followed did not: the
healthcheck respawned and then could never verify, because the impostor still held
the port. Every round burned ~22s on the restarter and ~23s on ops and achieved
nothing — for hours.

Both directions are pinned here, and both matter:

- a foreign occupant → report at ERROR, exit `EXIT_PORT_TAKEN`, and **do not
  respawn**;
- nothing listening → respawn exactly as before. That second one is the regression
  guard on the 98-minute-outage fix: a healthcheck that stops reviving dead daemons
  is a worse bug than the loop this replaces.

The verdicts come from the REAL `probe_daemon` against a REAL health server, not a
stub, so the classification itself is under test and not just the branch on it.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
from pathlib import Path

import pytest

from services.healthchecks import ops as ops_hc
from services.healthchecks import restarter as restarter_hc
from shared import daemon_health
from shared.config import settings
from shared.daemon_health import EXIT_PORT_TAKEN, DaemonProbe

# The healthcheck module under test, its daemon name, its `settings.services`
# pidfile attribute, and the respawn entry point a terminal verdict must not reach.
_CASES = [
    pytest.param(restarter_hc, "restarter", "restarter_pidfile", id="restarter"),
    pytest.param(ops_hc, "ops", "ops_pidfile", id="ops"),
]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(autouse=True)
def _quiet_and_inert(monkeypatch: pytest.MonkeyPatch) -> None:
    """`main()` calls init_gateway_process() and (restarter only) the DB-touching
    stand-in dispatch; neither is what these tests are about."""
    for mod in (restarter_hc, ops_hc):
        monkeypatch.setattr(mod, "init_gateway_process", lambda *_a, **_kw: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(restarter_hc, "_standin_dispatch", lambda: None)


def _point_at(
    mod: object, port: int, pidfile: Path, pidfile_attr: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Aim the healthcheck's probe at `port` and at a pidfile holding OUR pid.

    Both are read at call time (`_probe` looks up the module global and the settings
    field per call), so the real `probe_daemon` runs against the server the test
    started, with an identity that would otherwise verify."""
    pidfile.write_text(str(os.getpid()))
    monkeypatch.setattr(mod, "_HEALTH_URL", f"http://127.0.0.1:{port}/healthz")
    monkeypatch.setattr(settings.services, pidfile_attr, pidfile)


@pytest.mark.asyncio
@pytest.mark.parametrize(("mod", "name", "pidfile_attr"), _CASES)
async def test_a_foreign_clusters_daemon_on_the_port_is_terminal(
    mod: object,
    name: str,
    pidfile_attr: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """THE defect: the right daemon kind, answering, under a different `$AVA_HOME`.

    No respawn is attempted, the failure is reported at ERROR, and the exit code is
    the distinct one — so the watchdog's own "reported failure (exit 3)" line tells
    an operator this needs a human, not another round."""
    port = _free_port()
    server = await daemon_health.start_health_server(name, port=port)
    try:
        _point_at(mod, port, tmp_path / f"{name}.pid", pidfile_attr, monkeypatch)
        # The server bound with the real home; make the PROBE side believe it belongs
        # to a different cluster — the asymmetry a co-located foreign unit has.
        monkeypatch.setattr(daemon_health, "ava_home", lambda: tmp_path / "other-home")
        monkeypatch.setattr(
            mod, "_restart_daemon", lambda: pytest.fail("must not respawn against an impostor")
        )

        with caplog.at_level("ERROR"), pytest.raises(SystemExit) as excinfo:
            await asyncio.to_thread(mod.main)  # type: ignore[attr-defined]

        assert excinfo.value.code == EXIT_PORT_TAKEN
        logged = " ".join(r.getMessage() for r in caplog.records)
        assert "NOT REVIVABLE" in logged
        assert "other-home" in logged, "the operator needs to know WHICH home answered"
    finally:
        await daemon_health.stop_health_server(server)


@pytest.mark.parametrize(("mod", "name", "pidfile_attr"), _CASES)
def test_nothing_listening_still_respawns(
    mod: object,
    name: str,
    pidfile_attr: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression guard on the 98-minute outage's fix: a plain dead daemon —
    free port, nobody answering — must still be revived, exactly as before."""
    _point_at(mod, _free_port(), tmp_path / f"{name}.pid", pidfile_attr, monkeypatch)
    respawns: list[int] = []
    monkeypatch.setattr(
        mod, "_restart_daemon", lambda: (respawns.append(1), DaemonProbe.up("pid 1"))[1]
    )

    mod.main()  # type: ignore[attr-defined]

    assert respawns == [1]


@pytest.mark.parametrize(("mod", "name", "pidfile_attr"), _CASES)
def test_a_respawn_that_never_comes_up_reports_and_returns(
    mod: object,
    name: str,
    pidfile_attr: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ "Respawned and it did not come up" no longer exits (task #1941): the round
    reports the failure with the scheduled backoff delay and returns — the
    per-process backoff paces the retries, and the terminal exit code 3 stays the
    one code that means "a human must intervene"."""
    _point_at(mod, _free_port(), tmp_path / f"{name}.pid", pidfile_attr, monkeypatch)
    monkeypatch.setattr(mod, "_restart_daemon", lambda: DaemonProbe.down("healthz unreachable"))

    with caplog.at_level("WARNING"):
        mod.main()  # type: ignore[attr-defined] — no SystemExit
    assert (
        "daemon restart FAILED (healthz unreachable) — next respawn attempt in 60s" in caplog.text
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("mod", "name", "pidfile_attr"), _CASES)
async def test_the_terminal_state_self_clears_with_no_stored_state(
    mod: object,
    name: str,
    pidfile_attr: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing is persisted to remember the terminal verdict, so nothing has to be
    cleared when the occupant leaves: the very next round probes a free port, reads
    DOWN, and respawns normally.

    Same process, same monkeypatched wiring — only the impostor goes away between
    the two rounds."""
    port = _free_port()
    pidfile = tmp_path / f"{name}.pid"
    server = await daemon_health.start_health_server(name, port=port)
    try:
        _point_at(mod, port, pidfile, pidfile_attr, monkeypatch)
        monkeypatch.setattr(daemon_health, "ava_home", lambda: tmp_path / "other-home")
        monkeypatch.setattr(mod, "_restart_daemon", lambda: pytest.fail("must not respawn"))
        with pytest.raises(SystemExit) as excinfo:
            await asyncio.to_thread(mod.main)  # type: ignore[attr-defined]
        assert excinfo.value.code == EXIT_PORT_TAKEN
    finally:
        await daemon_health.stop_health_server(server)

    # Round two: the occupant is gone. No reset call, no state file, no flag.
    respawns: list[int] = []
    monkeypatch.setattr(
        mod, "_restart_daemon", lambda: (respawns.append(1), DaemonProbe.up("pid 1"))[1]
    )
    await asyncio.to_thread(mod.main)  # type: ignore[attr-defined]
    assert respawns == [1]


@pytest.mark.asyncio
async def test_the_restarter_still_dispatches_in_the_daemons_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Terminal means "stop respawning", not "stop remediating".

    With the restarter daemon unrevivable, its stand-in dispatch is the ONLY thing
    still moving `restarting` rows — which is precisely what stayed frozen for 98
    minutes on 2026-07-24. It runs on the terminal path too, where "until a human
    intervenes" is literal, even though no respawn was attempted."""
    port = _free_port()
    server = await daemon_health.start_health_server("restarter", port=port)
    ran: list[int] = []
    try:
        _point_at(restarter_hc, port, tmp_path / "restarter.pid", "restarter_pidfile", monkeypatch)
        monkeypatch.setattr(daemon_health, "ava_home", lambda: tmp_path / "other-home")
        monkeypatch.setattr(restarter_hc, "_standin_dispatch", lambda: ran.append(1))
        monkeypatch.setattr(restarter_hc, "_restart_daemon", lambda: pytest.fail("no respawn"))

        with contextlib.suppress(SystemExit):
            await asyncio.to_thread(restarter_hc.main)
    finally:
        await daemon_health.stop_health_server(server)

    assert ran == [1]
