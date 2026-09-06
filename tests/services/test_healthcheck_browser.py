"""ava-browser healthcheck — identity-verified CDP AND ava-browser session liveness.

Either answer alone lies. A CDP 200 cannot tell the supervised Chrome from an
orphan holding the same port, nor OUR Chrome from another unit's, and the daemon
refuses to launch while the port is served — so a CDP-only check stayed green
forever with no browser under supervision. These lock the (session, verdict)
matrix: the three verdicts `services.browser.probe.probe_browser` can return,
crossed with the session being alive or gone.

Two behavior changes are pinned here on top of the matrix:

- the orphan-of-ours branch (our Chrome holds the port, session gone) now
  CLOSES the loop itself — sweep the unsupervised Chrome (identity-verified
  ours) and rebuild the session — instead of naming an operator remedy every
  round for days;
- ERROR reporting is episode-gated: one line per condition episode (plus a
  reminder after `_EPISODE_REMINDER_S`), never one per round.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

import services.healthchecks.browser as hc
from shared.daemon_health import EXIT_PORT_TAKEN, EXIT_RESPAWN_FAILED, DaemonProbe


class _FakeClock:
    """Injectable wall clock for the episode record."""

    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


def _probes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    probe: DaemonProbe,
    session: bool,
) -> tuple[list[str], list[str], hc._Episode, _FakeClock]:
    """Wire probe/session/reap/restart to fixed answers; return the restarts, the
    reaped pids, the episode reporter and its clock. Every test gets a fresh
    state file (tmp_path) so no episode leaks between tests."""
    restarts: list[str] = []
    reaped: list[str] = []
    clock = _FakeClock()
    episode = hc._Episode(tmp_path / "browser.json", now=clock)

    def _restart() -> bool:
        restarts.append("restart")
        return True

    def _reap() -> list[int]:
        reaped.append("reap")
        return [7]

    monkeypatch.setattr(hc, "_probe", lambda: probe)
    monkeypatch.setattr(hc, "_session_alive", lambda: session)
    monkeypatch.setattr(hc.macos_readiness, "degraded_wait_reason", lambda: None)
    monkeypatch.setattr(hc, "init_gateway_process", lambda *_a, **_kw: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(hc, "_restart_daemon", _restart)
    monkeypatch.setattr(hc, "reap_cluster_chrome", _reap)
    monkeypatch.setattr(hc, "_episode_reporter", lambda: episode)
    return restarts, reaped, episode, clock


def test_healthy_when_session_and_our_chrome_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    restarts, reaped, *_ = _probes(
        monkeypatch, tmp_path, probe=DaemonProbe.up("chrome pid 1 on /p"), session=True
    )
    hc.main()
    assert restarts == []
    assert reaped == []


def test_restarts_when_both_down(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    restarts, *_ = _probes(
        monkeypatch, tmp_path, probe=DaemonProbe.down("CDP unreachable"), session=False
    )
    hc.main()
    assert restarts == ["restart"]


def test_restarts_when_session_up_but_cdp_down(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A live pane whose Chrome crashed or hung: respawn_service kills the stale
    session first, so the restart is still the right move."""
    restarts, *_ = _probes(
        monkeypatch, tmp_path, probe=DaemonProbe.down("CDP unreachable"), session=True
    )
    hc.main()
    assert restarts == ["restart"]


def test_waiting_for_macos_keychain_is_degraded_not_restarted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    restarts, *_ = _probes(
        monkeypatch, tmp_path, probe=DaemonProbe.down("waiting for macOS readiness"), session=True
    )
    monkeypatch.setattr(
        hc.macos_readiness,
        "degraded_wait_reason",
        lambda: "login Keychain is not ready",
    )
    with caplog.at_level(logging.WARNING, logger="services.healthchecks.browser"):
        hc.main()
    assert restarts == []
    assert any("DEGRADED" in record.getMessage() for record in caplog.records)


def test_orphan_of_our_own_is_swept_and_rebuilt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Our Chrome on the port with no session = an unsupervised browser of ours.
    The healthcheck now performs the operator remedy itself — sweep the orphan
    and rebuild the session — instead of repeating a 'run ava stop --stop-browser'
    ERROR every 60s forever (the 1,094-lines/day machine-1 shape)."""
    restarts, reaped, *_ = _probes(
        monkeypatch, tmp_path, probe=DaemonProbe.up("chrome pid 7 on /p"), session=False
    )
    with caplog.at_level(logging.INFO, logger="services.healthchecks.browser"):
        hc.main()
    assert reaped == ["reap"]
    assert restarts == ["restart"]
    assert any("sweeping the unsupervised Chrome" in r.getMessage() for r in caplog.records)
    assert any("session rebuilt" in r.getMessage() for r in caplog.records)


def test_orphan_with_nothing_left_to_reap_still_rebuilds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The Chrome died between the probe and the sweep — the port is already
    free, so the rebuild is all that is left and still runs."""
    restarts, *_ = _probes(
        monkeypatch, tmp_path, probe=DaemonProbe.up("chrome pid 7 on /p"), session=False
    )
    monkeypatch.setattr(hc, "reap_cluster_chrome", list)
    hc.main()
    assert restarts == ["restart"]


def test_orphan_sweep_raise_is_a_failed_heal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A sweep that raises must not crash the healthcheck; it reports through
    the episode gate and exits EXIT_RESPAWN_FAILED so the next round retries."""
    restarts, *_ = _probes(
        monkeypatch, tmp_path, probe=DaemonProbe.up("chrome pid 7 on /p"), session=False
    )
    monkeypatch.setattr(hc, "reap_cluster_chrome", _boom)

    with (
        caplog.at_level(logging.ERROR, logger="services.healthchecks.browser"),
        pytest.raises(SystemExit) as exc,
    ):
        hc.main()
    assert exc.value.code == EXIT_RESPAWN_FAILED
    assert restarts == []
    assert any("sweep + session rebuild FAILED" in r.getMessage() for r in caplog.records)


def _boom() -> list[int]:
    raise RuntimeError("process table exploded")


def test_orphan_heal_failure_is_reported_once_per_episode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The failed-heal ERROR is episode-gated: round one reports, round two with
    the same condition logs only DEBUG — while still RETRYING the heal."""
    _probes(monkeypatch, tmp_path, probe=DaemonProbe.up("chrome pid 7 on /p"), session=False)
    monkeypatch.setattr(hc, "reap_cluster_chrome", _boom)

    with (
        caplog.at_level(logging.ERROR, logger="services.healthchecks.browser"),
        pytest.raises(SystemExit),
    ):
        hc.main()
    first = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(first) == 1

    caplog.clear()
    with (
        caplog.at_level(logging.ERROR, logger="services.healthchecks.browser"),
        pytest.raises(SystemExit),
    ):
        hc.main()
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


def test_orphan_heal_retries_after_a_failed_round(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The episode record gates reporting, never the action: after a failed heal
    round, the next round sweeps and rebuilds again."""
    restarts, reaped, *_ = _probes(
        monkeypatch, tmp_path, probe=DaemonProbe.up("chrome pid 7 on /p"), session=False
    )
    monkeypatch.setattr(hc, "reap_cluster_chrome", _boom)
    with pytest.raises(SystemExit):
        hc.main()

    def _reap_ok() -> list[int]:
        reaped.append("reap")
        return [7]

    monkeypatch.setattr(hc, "reap_cluster_chrome", _reap_ok)
    hc.main()
    assert restarts == ["restart"]
    assert reaped == ["reap"]


def test_foreign_browser_is_terminal_and_never_respawned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The case a CDP-only probe could not even see: someone else's Chrome answers
    this cluster's port. No respawn can bind it, so report at ERROR and exit with
    the distinct terminal code the watchdog logs."""
    restarts, reaped, *_ = _probes(
        monkeypatch,
        tmp_path,
        probe=DaemonProbe.port_taken("identity mismatch on CDP :9222: no Chrome ..."),
        session=False,
    )
    with (
        caplog.at_level(logging.ERROR, logger="services.healthchecks.browser"),
        pytest.raises(SystemExit) as exc,
    ):
        hc.main()
    assert exc.value.code == EXIT_PORT_TAKEN
    assert restarts == []
    assert reaped == []
    assert any("NOT REVIVABLE" in r.getMessage() for r in caplog.records)


def test_foreign_browser_is_terminal_even_with_a_live_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The terminal question is asked BEFORE the session one: our session being
    alive does not make a respawn able to bind a port someone else holds (our
    Chrome lost the bind and is running with a dead DevTools endpoint)."""
    restarts, *_ = _probes(
        monkeypatch, tmp_path, probe=DaemonProbe.port_taken("another unit's browser"), session=True
    )
    with pytest.raises(SystemExit) as exc:
        hc.main()
    assert exc.value.code == EXIT_PORT_TAKEN
    assert restarts == []


def test_failed_restart_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The watchdog's "revive failed" signal survives the identity rewrite."""
    _probes(monkeypatch, tmp_path, probe=DaemonProbe.down("CDP unreachable"), session=False)
    monkeypatch.setattr(hc, "_restart_daemon", lambda: False)
    with (
        caplog.at_level(logging.ERROR, logger="services.healthchecks.browser"),
        pytest.raises(SystemExit) as exc,
    ):
        hc.main()
    assert exc.value.code == EXIT_RESPAWN_FAILED
    assert any("restart FAILED" in r.getMessage() for r in caplog.records)


# ─── episode-gated reporting ────────────────────────────────────────────────


def test_terminal_is_reported_once_per_episode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """THE noise fix for the win box: a permanent terminal verdict logged a fresh
    ERROR + a watchdog exit line every ~67s for hours. One ERROR per episode;
    quiet rounds keep the exit code (the watchdog de-duplicates its own line)."""
    _probes(
        monkeypatch,
        tmp_path,
        probe=DaemonProbe.port_taken("identity mismatch on CDP :9222"),
        session=False,
    )
    with (
        caplog.at_level(logging.ERROR, logger="services.healthchecks.browser"),
        pytest.raises(SystemExit) as exc,
    ):
        hc.main()
    assert exc.value.code == EXIT_PORT_TAKEN
    assert len([r for r in caplog.records if r.levelno >= logging.ERROR]) == 1

    caplog.clear()
    with (
        caplog.at_level(logging.ERROR, logger="services.healthchecks.browser"),
        pytest.raises(SystemExit) as exc,
    ):
        hc.main()
    assert exc.value.code == EXIT_PORT_TAKEN
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


def test_terminal_re_reports_when_the_episode_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A new failure episode after a healthy stretch is a new first sight — the
    recovery round cleared the record, so the next terminal round reports."""
    _, _, _, clock = _probes(
        monkeypatch, tmp_path, probe=DaemonProbe.up("chrome pid 1"), session=True
    )
    hc.main()  # healthy round: no episode open

    monkeypatch.setattr(hc, "_probe", lambda: DaemonProbe.port_taken("foreign"))
    with (
        caplog.at_level(logging.ERROR, logger="services.healthchecks.browser"),
        pytest.raises(SystemExit),
    ):
        hc.main()
    assert len([r for r in caplog.records if r.levelno >= logging.ERROR]) == 1
    clock.t += 10  # far under the reminder window — quiet round
    caplog.clear()
    with (
        caplog.at_level(logging.ERROR, logger="services.healthchecks.browser"),
        pytest.raises(SystemExit),
    ):
        hc.main()
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


def test_recovery_after_an_episode_logs_one_info_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The episode's end is worth one line: the operator sees the condition
    cleared, not just the ERRORs that opened it."""
    _, _, _, clock = _probes(
        monkeypatch, tmp_path, probe=DaemonProbe.port_taken("foreign"), session=False
    )
    with pytest.raises(SystemExit):
        hc.main()

    monkeypatch.setattr(hc, "_probe", lambda: DaemonProbe.up("chrome pid 1 on /p"))
    monkeypatch.setattr(hc, "_session_alive", lambda: True)
    with caplog.at_level(logging.INFO, logger="services.healthchecks.browser"):
        hc.main()
    assert any("browser recovered" in r.getMessage() for r in caplog.records)
    clock.t += 10

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="services.healthchecks.browser"):
        hc.main()
    assert not any("browser recovered" in r.getMessage() for r in caplog.records)


def test_terminal_reminder_re_reports_after_the_interval(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An episode that outlives the reminder window re-reports once per window —
    the condition never goes silent for good, it just stops shouting per round."""
    _, _, _, clock = _probes(
        monkeypatch, tmp_path, probe=DaemonProbe.port_taken("foreign"), session=False
    )
    with (
        caplog.at_level(logging.ERROR, logger="services.healthchecks.browser"),
        pytest.raises(SystemExit),
    ):
        hc.main()
    clock.t += hc._EPISODE_REMINDER_S - 1
    caplog.clear()
    with (
        caplog.at_level(logging.ERROR, logger="services.healthchecks.browser"),
        pytest.raises(SystemExit),
    ):
        hc.main()
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []

    clock.t += 2
    caplog.clear()
    with (
        caplog.at_level(logging.ERROR, logger="services.healthchecks.browser"),
        pytest.raises(SystemExit),
    ):
        hc.main()
    assert len([r for r in caplog.records if r.levelno >= logging.ERROR]) == 1


def test_corrupt_episode_record_fails_open(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An unreadable record must never suppress a report — fail open toward
    reporting, exactly like the probe's fail-closed-toward-respawn."""
    state_file = tmp_path / "browser.json"
    state_file.write_text("{not json")
    _probes(monkeypatch, tmp_path, probe=DaemonProbe.port_taken("foreign"), session=False)
    with (
        caplog.at_level(logging.ERROR, logger="services.healthchecks.browser"),
        pytest.raises(SystemExit),
    ):
        hc.main()
    assert len([r for r in caplog.records if r.levelno >= logging.ERROR]) == 1
