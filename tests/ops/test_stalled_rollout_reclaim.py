"""The stalled-rollout reclaim — its evidence, its two stages, and where it runs.

The 2026-08-02 incident in one sentence: an `ava-rollout` orchestration hung inside
`converge` on a `codesign` child waiting for a GUI authorization, held the cluster
stopped for 67 minutes, and every self-healing layer stood down because each of them
asks whether the lock holder is *alive* rather than whether it is getting anywhere.

Three things have to be true for that not to repeat, and they are the three sections
below: the stall has to be *detected* without misjudging a healthy rollout, the
reclaim has to *recover* rather than merely kill, and the whole thing has to run on a
`cluster_paused` host — the state a rollout puts this box into before it does anything
that could hang.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import os
import time
from pathlib import Path

import pytest

from ops.controllers import stalled_rollout as sr
from ops.controllers.base import BlockScope
from ops.controllers.stalled_rollout import StalledRolloutController
from shared.last_update import LastUpdate, UpdateOutcome

# The autouse `_guard_cluster_spawn` stubs `reclaim_stalled_rollout_if_hung` into an
# assertion: it signals or kills a real orchestration, and on a dev box the record it
# reads is prod's. This module is that function's own test.
pytestmark = pytest.mark.real_cluster_spawn

_HOLDER = "gateway-host:pid4242"
_MACHINE = "gateway-host"


class _Backend:
    """Session backend stub. Records kills; `kills` is what `kill_session` reports."""

    def __init__(self, *, kills: bool = True) -> None:
        self.kills = kills
        self.killed: list[str] = []

    def kill_session(
        self, name: str, *, graceful: bool = False, expected: bool = False
    ) -> tuple[bool, str]:
        self.killed.append(name)
        return (self.kills, "forced")


class _Signals:
    """Every `os.kill` this module sent, as (pid, signal)."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, int]] = []

    def __call__(self, pid: int, sig: int) -> None:
        self.sent.append((pid, sig))


def _record(
    *,
    log: Path | None,
    started_s_ago: float,
    outcome: UpdateOutcome = UpdateOutcome.RUNNING,
    holder: str = _HOLDER,
) -> LastUpdate:
    started = dt.datetime.fromtimestamp(time.time() - started_s_ago, tz=dt.UTC)
    return LastUpdate(
        outcome=outcome,
        failed=False,
        holder=holder,
        started_at=started,
        log_path=str(log) if log is not None else None,
    )


def _write_log(path: Path, *, age_s: float) -> Path:
    path.write_text("[rollout] Phase B\n")
    stamp = time.time() - age_s
    os.utime(path, (stamp, stamp))
    return path


@pytest.fixture
def rollout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Signals:
    """A gateway host whose `ava-rollout` session is alive, whose holder pid is alive
    and local, and whose `os.kill` is recorded rather than sent."""
    monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path)
    monkeypatch.setattr("ops.cluster_session._has_orchestration_session", lambda _name: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("shared.machine.machine_name", lambda: _MACHINE)
    monkeypatch.setattr("shared.proc.process_alive", lambda _pid: True)  # pyright: ignore[reportUnknownArgumentType]
    signals = _Signals()
    monkeypatch.setattr(os, "kill", signals)
    return signals


def _serve(monkeypatch: pytest.MonkeyPatch, record: LastUpdate | None) -> None:
    monkeypatch.setattr(sr, "read_last_update", lambda: record)


# ─── detection: what counts as "stopped making progress" ─────────────────────


def test_a_rollout_whose_log_is_advancing_is_left_alone(
    rollout: _Signals, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = _write_log(tmp_path / "rollout-1785327000.log", age_s=30.0)
    _serve(monkeypatch, _record(log=log, started_s_ago=3600.0))

    assert sr.reclaim_stalled_rollout_if_hung() is False
    assert rollout.sent == []


def test_a_rollout_whose_log_has_stalled_is_interrupted(
    rollout: _Signals, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = _write_log(tmp_path / "rollout-1785327000.log", age_s=sr._ROLLOUT_STALL_TIMEOUT_S + 60.0)
    _serve(monkeypatch, _record(log=log, started_s_ago=7200.0))

    import signal as _signal

    assert sr.reclaim_stalled_rollout_if_hung() is True
    assert rollout.sent == [(4242, _signal.SIGINT)]


def test_a_young_rollout_that_has_not_written_yet_is_not_stalled(
    rollout: _Signals, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The false positive that `started_at` exists to prevent, and the reason this
    cannot be built on "the newest `rollout-*.log` in the log directory" the way the
    hung-updater reaper is.

    `spawn_rollout` creates the log *path* and the pane's `tee` creates the *file*, so
    between the session spawning and the shell's first write the newest log on
    disk is the PREVIOUS rollout's — hours old, and sitting right where a glob would
    find it. Reading that as this session's silence would interrupt a healthy rollout
    in its first second, which is strictly worse than missing a hung one.
    """
    _write_log(tmp_path / "rollout-1785000000.log", age_s=sr._ROLLOUT_STALL_TIMEOUT_S * 10)
    fresh = tmp_path / "rollout-1785327000.log"  # named by the record, not yet written
    _serve(monkeypatch, _record(log=fresh, started_s_ago=2.0))

    assert sr.reclaim_stalled_rollout_if_hung() is False
    assert rollout.sent == []


def test_a_terminated_rollouts_record_is_never_acted_on(
    rollout: _Signals, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a `RUNNING` row — an open record whose holder still holds the deploy lease
    — describes an orchestration that is executing. A finished rollout's row names a
    pid that has since been recycled, and its log is stale by construction."""
    log = _write_log(tmp_path / "rollout-1785327000.log", age_s=sr._ROLLOUT_STALL_TIMEOUT_S * 5)
    _serve(monkeypatch, _record(log=log, started_s_ago=7200.0, outcome=UpdateOutcome.CLEAN))

    assert sr.reclaim_stalled_rollout_if_hung() is False
    assert rollout.sent == []


def test_a_holder_on_another_machine_is_never_signalled(
    rollout: _Signals,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`<machine>:pid<N>` is only meaningful in its own host's pid namespace. Signalling
    a pid parsed out of somebody else's holder string would hit an unrelated local
    process — a far worse outcome than leaving the rollout alone."""
    log = _write_log(tmp_path / "rollout-1785327000.log", age_s=sr._ROLLOUT_STALL_TIMEOUT_S + 60.0)
    _serve(monkeypatch, _record(log=log, started_s_ago=7200.0, holder="wsl:pid4242"))

    with caplog.at_level("ERROR"):
        assert sr.reclaim_stalled_rollout_if_hung() is False
    assert rollout.sent == []
    assert any("does not name a live process" in r.getMessage() for r in caplog.records)


def test_an_unreadable_record_defers_instead_of_acting(
    rollout: _Signals, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rollout can take the data plane down as part of failing. Reading nothing is
    not evidence of a stall, and the round costs one minute to ask again."""

    def _boom() -> LastUpdate:
        raise RuntimeError("connection refused")

    monkeypatch.setattr(sr, "read_last_update", _boom)

    assert sr.reclaim_stalled_rollout_if_hung() is False
    assert rollout.sent == []


def test_no_rollout_session_means_nothing_to_reclaim(
    rollout: _Signals, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Freshness alone says nothing: every host that has ever rolled out keeps its
    `rollout-*.log` forever, and it ages past the timeout within the hour."""
    monkeypatch.setattr("ops.cluster_session._has_orchestration_session", lambda _name: False)  # pyright: ignore[reportUnknownArgumentType]
    log = _write_log(tmp_path / "rollout-1785327000.log", age_s=sr._ROLLOUT_STALL_TIMEOUT_S * 100)
    _serve(monkeypatch, _record(log=log, started_s_ago=7200.0))

    assert sr.reclaim_stalled_rollout_if_hung() is False
    assert rollout.sent == []


def test_the_stall_clock_is_the_familys_one_number() -> None:
    """Same argument as the hung-updater reaper's: two clocks that disagree about "has
    this stopped making progress" are two chances to declare a working rollout dead."""
    from shared.deploy_timing import NO_PROGRESS_TIMEOUT_S

    assert sr._ROLLOUT_STALL_TIMEOUT_S == NO_PROGRESS_TIMEOUT_S


def test_phase_b_narrates_itself_so_a_slow_fanout_is_not_read_as_a_stall() -> None:
    """The one phase that was legitimately allowed to be silent for the whole stall
    window, which would have put a healthy slow rollout exactly on this reaper's
    boundary. `_probe_one_until_unpaused` prints nothing per pass by design, so the
    heartbeat rides the lease-renewal task that already runs beside the poll — and it
    has to stay far inside the bound, not merely under it."""
    import cli.commands.update as update_mod
    from shared.deploy_timing import LEASE_RENEW_INTERVAL_S

    assert update_mod._POLL_TIMEOUT_S == sr._ROLLOUT_STALL_TIMEOUT_S
    assert LEASE_RENEW_INTERVAL_S * 5 < sr._ROLLOUT_STALL_TIMEOUT_S


async def test_the_phase_b_heartbeat_reaches_the_rollout_log(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """And it goes to the process's own stdio, which is what the reaper measures: the
    rollout log IS this process's stdout+stderr, teed there by `spawn_rollout`. A
    heartbeat that went to a logger's file instead would leave the poll silent on the
    one surface being read."""
    import cli.commands.update as update_mod

    monkeypatch.setattr("shared.deploy_timing.LEASE_RENEW_INTERVAL_S", 0.0)
    monkeypatch.setattr("shared.cluster_lock.renew_update_lock", lambda _holder: True)  # pyright: ignore[reportUnknownArgumentType]

    task = asyncio.ensure_future(update_mod._renew_lease_while_polling(_HOLDER))
    seen = ""
    try:
        for _ in range(20):
            await asyncio.sleep(0)
            seen += capsys.readouterr().err
            if "still polling Phase B" in seen:
                break
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert "still polling Phase B" in seen


# ─── the reclaim: interrupt first, because the recovery is inside the process ──


def test_the_first_move_is_a_signal_and_not_a_kill(
    rollout: _Signals, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of the two stages. `cli.commands.update`'s `finally` is what
    unpauses this host, dials every paused agent-runner back to resumed and records the
    terminal outcome — a force-kill destroys the only process that can run it
    and leaves the cluster exactly where the hang left it. `SIGINT` becomes a
    `KeyboardInterrupt`, which no `except Exception:` swallows, so that `finally` runs.
    """
    backend = _Backend()
    monkeypatch.setattr("shared.session_backend.get_backend", lambda: backend)
    log = _write_log(tmp_path / "rollout-1785327000.log", age_s=sr._ROLLOUT_STALL_TIMEOUT_S + 60.0)
    _serve(monkeypatch, _record(log=log, started_s_ago=7200.0))

    assert sr.reclaim_stalled_rollout_if_hung() is True
    assert backend.killed == []  # nothing was killed
    assert len(rollout.sent) == 1


def test_a_rollout_that_recovers_after_the_interrupt_is_not_killed(
    rollout: _Signals, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The designed path end to end: the interrupt lands, the orchestration's `finally`
    starts narrating its abort into the same log, and the next round sees a log that is
    moving again. Stage 2 must never be reached — the recovery IS progress."""
    backend = _Backend()
    monkeypatch.setattr("shared.session_backend.get_backend", lambda: backend)
    path = tmp_path / "rollout-1785327000.log"
    log = _write_log(path, age_s=sr._ROLLOUT_STALL_TIMEOUT_S + 60.0)
    _serve(monkeypatch, _record(log=log, started_s_ago=7200.0))

    assert sr.reclaim_stalled_rollout_if_hung() is True
    _write_log(path, age_s=1.0)  # the `finally` is printing its aftermath

    assert sr.reclaim_stalled_rollout_if_hung() is False
    assert backend.killed == []


def test_the_interrupt_is_sent_once_and_not_every_round(
    rollout: _Signals, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second interrupt would land *inside* the recovery the first one started —
    aborting the compensating resume part way through the fleet, which is the residual
    state the whole mechanism exists to avoid. The heal record is keyed on the run, so
    "already interrupted" stays true for as long as that run exists."""
    backend = _Backend()
    monkeypatch.setattr("shared.session_backend.get_backend", lambda: backend)
    log = _write_log(tmp_path / "rollout-1785327000.log", age_s=sr._ROLLOUT_STALL_TIMEOUT_S + 60.0)
    _serve(monkeypatch, _record(log=log, started_s_ago=7200.0))

    assert sr.reclaim_stalled_rollout_if_hung() is True
    assert sr.reclaim_stalled_rollout_if_hung() is False  # still silent, still in its window
    assert len(rollout.sent) == 1
    assert backend.killed == []


def test_a_rollout_that_ignored_the_interrupt_is_force_killed(
    rollout: _Signals,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Stage 2. A process group that has ignored an interrupt for a further full
    no-progress window is not going to run its `finally`, and its session alone refuses
    every `ava update` cluster-wide (`current_orchestration`) and blocks this host's
    pin / code / pause controllers. The log says plainly that the abort did NOT run."""
    backend = _Backend()
    monkeypatch.setattr("shared.session_backend.get_backend", lambda: backend)
    log = _write_log(tmp_path / "rollout-1785327000.log", age_s=sr._ROLLOUT_STALL_TIMEOUT_S + 60.0)
    _serve(monkeypatch, _record(log=log, started_s_ago=7200.0))

    assert sr.reclaim_stalled_rollout_if_hung() is True  # stage 1
    _age_the_interrupt(tmp_path, by_s=sr._ROLLOUT_STALL_TIMEOUT_S + 60.0)

    with caplog.at_level("ERROR"):
        assert sr.reclaim_stalled_rollout_if_hung() is True  # stage 2
    assert backend.killed == ["ava-rollout"]
    assert len(rollout.sent) == 1  # not re-interrupted on the way to the kill
    killed_line = next(r.getMessage() for r in caplog.records if "force-killed" in r.getMessage())
    assert "own abort did NOT run" in killed_line
    assert "stranded-pause recovery" in killed_line


def test_a_new_rollout_starts_at_stage_one_however_the_last_one_ended(
    rollout: _Signals, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The record is dropped the moment no rollout session exists. Left behind, it
    would make the NEXT hung rollout skip its interrupt and go straight to the kill —
    the reclaim silently degrading to the crude path over time."""
    backend = _Backend()
    monkeypatch.setattr("shared.session_backend.get_backend", lambda: backend)
    log = _write_log(tmp_path / "rollout-1785327000.log", age_s=sr._ROLLOUT_STALL_TIMEOUT_S + 60.0)
    _serve(monkeypatch, _record(log=log, started_s_ago=7200.0))
    assert sr.reclaim_stalled_rollout_if_hung() is True

    monkeypatch.setattr("ops.cluster_session._has_orchestration_session", lambda _name: False)  # pyright: ignore[reportUnknownArgumentType]
    assert sr.reclaim_stalled_rollout_if_hung() is False  # clears the record

    monkeypatch.setattr("ops.cluster_session._has_orchestration_session", lambda _name: True)  # pyright: ignore[reportUnknownArgumentType]
    log2 = _write_log(tmp_path / "rollout-1785400000.log", age_s=sr._ROLLOUT_STALL_TIMEOUT_S + 60.0)
    _serve(monkeypatch, _record(log=log2, started_s_ago=7200.0, holder="gateway-host:pid5555"))

    assert sr.reclaim_stalled_rollout_if_hung() is True
    assert len(rollout.sent) == 2  # interrupted, not killed
    assert backend.killed == []


def _age_the_interrupt(home: Path, *, by_s: float) -> None:
    """Backdate the recorded interrupt so the next call sees its window elapsed."""
    import json

    path = home / "rollout_reclaim_attempt"
    record = json.loads(path.read_text())
    record["ts"] = record["ts"] - by_s
    path.write_text(json.dumps(record))


# ─── where it runs: the round is blocked by the pause the rollout itself set ──


def test_the_controller_acts_without_blocking_the_round(
    rollout: _Signals, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interrupting hands the host to the controllers that own the dimensions the
    rollout left wrong — stranded-pause lifts the pause, code restarts stale processes
    — and none of them can act while this round is short-circuited."""
    log = _write_log(tmp_path / "rollout-1785327000.log", age_s=sr._ROLLOUT_STALL_TIMEOUT_S + 60.0)
    _serve(monkeypatch, _record(log=log, started_s_ago=7200.0))

    result = StalledRolloutController().reconcile("gateway")

    assert result.acted is True
    assert result.blocks is BlockScope.NONE
    assert len(rollout.sent) == 1


def test_the_agent_runner_watchdog_does_not_reclaim(monkeypatch: pytest.MonkeyPatch) -> None:
    """The `ava-rollout` session and the log that is its progress signal are on the
    host that spawned them. A runner has neither and must keep deferring to the deploy
    lease — that deference is what holds the fleet still through a healthy rollout. The
    role check is ahead of every probe, so the runner leg costs nothing."""

    def _never(_name: str) -> bool:
        raise AssertionError("the agent-runner watchdog must not probe for a rollout session")

    monkeypatch.setattr("ops.cluster_session._has_orchestration_session", _never)

    result = StalledRolloutController().reconcile("agent-runner")

    assert result.acted is False
    assert result.blocks is BlockScope.NONE


async def test_the_reclaim_runs_on_a_paused_host_before_the_pause_ends_the_round(
    rollout: _Signals, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The trap the 2026-08-02 incident sprang, asserted against the DEFAULT controller
    list rather than against this controller alone.

    A rollout pauses this host in Phase A, before it reaches anything that can hang, so
    a hung rollout is *always* found on a paused host — and `PauseController` blocks the
    round with `BlockScope.ALL`, short-circuiting every controller behind it. That is
    not a hypothetical: the only line prod's watchdog emitted through all 67 minutes was
    `round blocked by pause (scope=all)`. A reclaim anywhere after `pause` in this list
    would never have run.
    """
    from datetime import UTC, datetime

    from ops.manager import ControllerManager
    from shared.host_deploy_state import HostDeployState

    state = HostDeployState(
        machine="laptop-host",
        posture="paused",
        updated_at=datetime.now(UTC),  # fresh: paused, and not yet stranded
        updater_lease_expires_at=None,
    )
    monkeypatch.setattr(
        "shared.host_deploy_state.read",
        lambda machine=None, **_kw: state,  # noqa: ARG005  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr("ops.cluster.reap_stalled_updater_if_hung", lambda: False)
    log = _write_log(tmp_path / "rollout-1785327000.log", age_s=sr._ROLLOUT_STALL_TIMEOUT_S + 60.0)
    _serve(monkeypatch, _record(log=log, started_s_ago=7200.0))

    manager = ControllerManager()  # the DEFAULT list — the order under test
    blocks = await manager.reconcile("gateway")

    assert len(rollout.sent) == 1  # reclaimed despite the pause behind it
    assert blocks is BlockScope.ALL  # and the pause still owns the round


def test_the_reclaim_is_ordered_ahead_of_the_pause_gate() -> None:
    """The ordering above, stated as the property rather than observed through one
    scenario — so a future reordering fails here with the reason attached."""
    from ops.controllers.stranded_pause import PauseController
    from ops.manager import build_controllers

    names = [type(c).__name__ for c in build_controllers()]
    assert names.index(StalledRolloutController.__name__) < names.index(PauseController.__name__)
