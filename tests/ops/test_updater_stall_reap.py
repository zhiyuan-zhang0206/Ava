"""The hung-updater reaper — its evidence, and the clock it runs on.

A live `ava-updater` session that stops renewing is hung, not slow, and it blocks every
future update / rollout / restart / recover on that host until something kills it. The
reaper's evidence is the updater lease (R1, Task #1021) — a live lease means "still
working", an expired one means hung — which replaced the log-mtime freshness judgment
that could not fire on Windows at all (the old-signal sweep, PR5, retired the
log-mtime fallback).

The second half of the file covers the schedule: the reap also runs once a watchdog
round, because "the next update clears it" is not true — the corpse makes
`current_orchestration()` answer `"update"`, which refuses that next update
cluster-wide before it can reach the reap.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from ops import cluster as cluster_mod
from ops import cluster_deploy, cluster_pause, cluster_session
from ops.controllers.base import BlockScope
from ops.controllers.stalled_updater import StalledUpdaterController

# The autouse `_guard_cluster_spawn` stubs `reap_stalled_updater_if_hung` into an
# assertion, because every agent-runner watchdog round calls it and its job is to
# force-kill a live session. This module is that function's own test.
pytestmark = pytest.mark.real_cluster_spawn


@pytest.fixture
def logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated `$AVA_HOME/logs`, plus a backend that reports no session log (the
    backend shape) unless a test overrides it."""
    monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    return log_dir


class _Backend:
    """A session backend stub: `log` is what it claims to redirect output to (None =
    a session backend that keeps no log file). Records kills."""

    def __init__(self, log: Path | None = None, *, kills: bool = True) -> None:
        self.log = log
        self.kills = kills
        self.killed: list[str] = []

    def session_log_path(self, name: str) -> Path | None:
        return self.log

    def kill_session(
        self, name: str, *, graceful: bool = False, expected: bool = False
    ) -> tuple[bool, str]:
        # `mode` is "forced" either way on the force path — the real backend
        # returns `(rc == 0, "forced")`, and a force-kill reports failure
        # both for a session it could not kill and for one that is already gone.
        self.killed.append(name)
        return (self.kills, "forced")


def _write(path: Path, *, age_s: float) -> Path:
    path.write_text("[updater] working\n")
    stamp = time.time() - age_s
    import os

    os.utime(path, (stamp, stamp))
    return path


def _use(monkeypatch: pytest.MonkeyPatch, backend: _Backend) -> None:
    # The updater session lives on the service session backend (S7) — the
    # reaper's kill goes to get_backend(), not get_backend()'s PTY sibling (the
    # shell backend).
    monkeypatch.setattr("shared.session_backend.get_backend", lambda: backend)


def _lease(
    monkeypatch: pytest.MonkeyPatch,
    *,
    expires_in_s: float | None,
    armed_before_pause: bool = False,
) -> None:
    """Point the reaper's liveness read at a lease: `expires_in_s` is the lease's
    remaining life (None = no lease row / no lease at all).

    `paused_at` is placed *relative to the lease's arming* rather than pinned to
    now, because the two are not independent facts: a lease this pause window armed
    is armed after the pause, and the row can only be read for a stop by dating the
    arming (`HostDeployState.updater_expired`). `armed_before_pause=True` builds the
    other real row — a previous run that ended without clearing, whose expiry the
    next update's pause inherits.
    """
    from datetime import UTC, datetime, timedelta

    from shared.host_deploy_state import UPDATER_LEASE_TTL_S, HostDeployState

    now = datetime.now(UTC)
    if expires_in_s is None:
        expires_at, paused_at, posture = None, now, "paused"
    else:
        expires_at = now + timedelta(seconds=expires_in_s)
        armed = expires_at - timedelta(seconds=UPDATER_LEASE_TTL_S)
        offset = timedelta(seconds=1)
        paused_at = armed + offset if armed_before_pause else armed - offset
        posture = "paused" if armed_before_pause else "converging"
    state = HostDeployState(
        machine="test",
        posture=posture,
        updated_at=now,
        updater_lease_expires_at=expires_at,
        paused_at=paused_at,
    )
    monkeypatch.setattr("shared.host_deploy_state.read", lambda *_a, **_k: state)  # pyright: ignore[reportUnknownArgumentType]


@pytest.fixture
def alive(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Whether `ava-updater` is alive; flip element 0 to change the answer.

    Both halves of the file need it: the scheduled reap asks before it does anything,
    and the caller-side one re-asks after a kill the backend reported as failed."""
    state = [True]
    monkeypatch.setattr(cluster_session, "_has_orchestration_session", lambda _name: state[0])  # pyright: ignore[reportUnknownArgumentType]
    return state


def test_a_live_lease_is_not_hung(logs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A lease with life left means "still working" — the lease-expiry judgment."""
    backend = _Backend()
    _use(monkeypatch, backend)
    _lease(monkeypatch, expires_in_s=600.0)

    # The liveness judgment says alive; the reap is only ever called on a hung
    # verdict (the caller gates on `_updater_hung`), so no kill happens here.
    assert cluster_mod._updater_hung("ava-updater") is False
    assert backend.killed == []


def test_an_expired_lease_is_reaped(logs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _Backend()
    _use(monkeypatch, backend)
    _lease(monkeypatch, expires_in_s=-60.0)

    assert cluster_mod._updater_hung("ava-updater") is True
    assert cluster_mod._reap_stalled_updater("ava-updater") is True
    assert backend.killed == ["ava-updater"]


def test_a_previous_runs_uncleared_lease_does_not_condemn_a_fresh_session(
    logs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The expiry in the row can belong to the LAST update rather than this one:
    nothing clears the column on the way into a pause (`set_posture` owns posture
    alone, deliberately), so a run that ended without clearing leaves it there. Read
    naively, the gap between this update's pause and its updater's first touch — a
    session spawn plus a Python cold start — is then a window in which the just-spawned
    session reads as hung and gets force-killed by the controller that is supposed to
    protect it."""
    backend = _Backend()
    _use(monkeypatch, backend)
    _lease(monkeypatch, expires_in_s=-60.0, armed_before_pause=True)

    assert cluster_mod._updater_hung("ava-updater") is False
    assert backend.killed == []


def test_a_lease_less_session_is_not_judged(logs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No lease = no judgment (a lease write that failed at entry, or an updater
    spawned before the lease existed): the session is left alone — killing on
    missing evidence is the worse mistake. This is the lease-only reading the
    old-signal sweep (PR5) left behind after retiring the log-mtime fallback,
    which was structurally unable to fire on Windows anyway."""
    backend = _Backend()
    _use(monkeypatch, backend)
    _lease(monkeypatch, expires_in_s=None)
    _write(logs / "updater-1785327000.log", age_s=5.0)

    assert cluster_mod._updater_hung("ava-updater") is False
    assert backend.killed == []

    # An old log changes nothing: without a lease there is nothing to expire.
    _write(logs / "updater-1785327000.log", age_s=cluster_mod._UPDATER_STALL_TIMEOUT_S + 60.0)
    assert cluster_mod._updater_hung("ava-updater") is False
    assert backend.killed == []


def test_a_live_lease_wins_over_any_log_evidence(
    logs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The retired judgment stays retired: arbitrary old log TEXT beside a live
    lease is not evidence (the file-mtime comparison is gone with the old-signal
    sweep). Stage MARKERS are the new evidence — see the stage-bound tests — and a
    log that carries none proves nothing."""
    backend = _Backend()
    _use(monkeypatch, backend)
    _lease(monkeypatch, expires_in_s=600.0)
    _write(logs / "updater-1785000000.log", age_s=cluster_mod._UPDATER_STALL_TIMEOUT_S + 999.0)

    assert cluster_mod._updater_hung("ava-updater") is False
    assert backend.killed == []


def test_a_live_lease_loses_to_a_stage_stuck_beyond_the_bound(
    logs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The P1 (2026-08-30) shape: the updater lease is one write at the run's start,
    so a host hung inside `uv` (a stalled download on the Windows runner) read as
    "still working" until the lease expired — its host only reaped it at the full
    bound. The updater's own stage markers are the progress fact: a current stage in
    flight beyond STAGE_NO_PROGRESS_TIMEOUT_S is hung however young the lease is.
    Same bound, same evidence as the Phase-B poll's POLL_NO_PROGRESS verdict."""
    from ops.updater_outcome import UpdaterOutcome

    backend = _Backend()
    _use(monkeypatch, backend)
    _lease(monkeypatch, expires_in_s=600.0)
    monkeypatch.setattr(
        "ops.updater_outcome.last_updater_outcome",
        lambda: UpdaterOutcome(
            kind="unknown", log="updater-178.log", current_stage="uv", current_stage_s=700.0
        ),
    )

    assert cluster_mod._updater_hung("ava-updater") is True


def test_a_stage_still_inside_the_bound_is_working(
    logs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The slow-but-legitimate Windows leg: the same stage name, an age below the
    bound — working, and the live lease's word stands. A measured win `uv` took
    449 s; the bound sits at 1.5x that, and a stage inside it is never reaped."""
    from ops.updater_outcome import UpdaterOutcome

    backend = _Backend()
    _use(monkeypatch, backend)
    _lease(monkeypatch, expires_in_s=600.0)
    monkeypatch.setattr(
        "ops.updater_outcome.last_updater_outcome",
        lambda: UpdaterOutcome(
            kind="unknown", log="updater-178.log", current_stage="uv", current_stage_s=100.0
        ),
    )

    assert cluster_mod._updater_hung("ava-updater") is False
    assert backend.killed == []


def test_stage_evidence_without_a_current_stage_proves_nothing(
    logs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `t=` marker in the tail means no in-flight stage to judge: an old commit's
    log, an unreadable one, or a stage that just completed (tail ends in `dur=`).
    Missing evidence is never a kill."""
    from ops.updater_outcome import UpdaterOutcome

    backend = _Backend()
    _use(monkeypatch, backend)
    _lease(monkeypatch, expires_in_s=600.0)
    monkeypatch.setattr(
        "ops.updater_outcome.last_updater_outcome",
        lambda: UpdaterOutcome(kind="unknown", log="updater-178.log"),
    )

    assert cluster_mod._updater_hung("ava-updater") is False
    assert backend.killed == []


def test_an_idle_host_is_never_judged_on_stage_evidence(
    logs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Posture idle means the transition finished; the only lingering shape there is
    the ladder's final `done` marker behind a stuck lease clear, which a reap would
    not improve. The stage judgment is scoped to hosts still mid-transition."""
    from datetime import UTC, datetime, timedelta

    from ops.updater_outcome import UpdaterOutcome
    from shared.host_deploy_state import HostDeployState

    backend = _Backend()
    _use(monkeypatch, backend)
    now = datetime.now(UTC)

    def _idle_row(_machine: str | None = None, **_k: object) -> HostDeployState:
        return HostDeployState(
            machine="test",
            posture="idle",
            updated_at=now,
            updater_lease_expires_at=now + timedelta(seconds=600),
            paused_at=now,
        )

    monkeypatch.setattr("shared.host_deploy_state.read", _idle_row)
    monkeypatch.setattr(
        "ops.updater_outcome.last_updater_outcome",
        lambda: UpdaterOutcome(
            kind="unknown", log="updater-178.log", current_stage="done", current_stage_s=700.0
        ),
    )

    assert cluster_mod._updater_hung("ava-updater") is False


def test_the_reap_clears_the_lease_it_killed(logs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A killed updater cannot clear its own lease, and the lease is what keeps the
    host reading "live updater" (and the Phase-B poll reading "still working") —
    one write at the run's start, armed for the whole bound. The reap clears it, so
    the host stops claiming liveness the moment the session is gone (P1,
    2026-08-30). Fail-soft: a DB that is down only means the row keeps its expiry."""
    backend = _Backend()
    _use(monkeypatch, backend)
    _lease(monkeypatch, expires_in_s=-60.0)
    cleared: list[int] = []
    monkeypatch.setattr("shared.host_deploy_state.clear_updater_lease", lambda: cleared.append(1))

    assert cluster_mod._reap_stalled_updater("ava-updater") is True
    assert backend.killed == ["ava-updater"]
    assert cleared == [1]


# ─── a failed kill is ambiguous, and the two readings need opposite answers ───


def test_a_session_that_survives_its_own_kill_is_not_reported_as_reaped(
    logs: Path, alive: list[bool], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`kill_session` answers `(ok, mode)` and can say no without raising. Believing
    it anyway would tell `spawn_update` to spawn over a session that is still there,
    and would make the watchdog round claim a successful reap every 60 s forever."""
    backend = _Backend(kills=False)
    _use(monkeypatch, backend)
    _lease(monkeypatch, expires_in_s=-60.0)

    with caplog.at_level("ERROR"):
        assert cluster_mod._reap_stalled_updater("ava-updater") is False
    assert backend.killed == ["ava-updater"]  # it was attempted
    assert any("kill it by hand" in r.getMessage() for r in caplog.records)


def test_an_updater_that_exits_between_the_check_and_the_kill_counts_as_cleared(
    logs: Path, alive: list[bool], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The benign half of the same `ok=False`. A force-kill reports failure when
    it cannot find the target, which is exactly what a session that finished on its
    own between the caller's liveness check and the kill looks like. Reading that as a
    failure would make `spawn_update` refuse with `ClusterUpdateInProgress` naming a
    session that is not there — blocking an update for no reason at all. So the
    verdict is settled by re-asking liveness, not by the exit code."""
    backend = _Backend(kills=False)
    _use(monkeypatch, backend)
    _lease(monkeypatch, expires_in_s=-60.0)
    alive[0] = False  # gone by the time the kill lands

    assert cluster_mod._updater_hung("ava-updater") is True
    assert cluster_mod._reap_stalled_updater("ava-updater") is True
    assert backend.killed == ["ava-updater"]


def test_the_inline_reap_log_does_not_claim_a_kill_on_the_recheck_path(
    logs: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`spawn_update`'s own reap reads the same ambiguous True the watchdog's does, so
    it has to describe it the same way. On this path the kill reported failure and the
    session turned out to be already gone — nothing was killed, and a deploy log that
    says "reaped" sends whoever is reading it looking for a process this host took
    down. The half that is true either way ("proceeding with new update") stays."""
    backend = _Backend(kills=False)
    _use(monkeypatch, backend)
    _lease(monkeypatch, expires_in_s=-60.0)
    # Alive for spawn_update's liveness check, gone by the recheck after the kill.
    answers = iter([True, False])

    def _session_alive(_name: str) -> bool:
        return next(answers, False)

    monkeypatch.setattr(cluster_session, "_has_orchestration_session", _session_alive)
    # The pause and the spawn are a different mechanism; this test is the log line
    # between them, and neither may run for real.
    monkeypatch.setattr(cluster_pause, "pause_local_cluster", lambda: None)
    monkeypatch.setattr(cluster_session, "_spawn_detached_session", lambda *_a, **_k: None)  # pyright: ignore[reportUnknownArgumentType]

    with caplog.at_level("WARNING"):
        cluster_deploy.spawn_update(restart_only=True)

    messages = [r.getMessage() for r in caplog.records]
    line = next(m for m in messages if "proceeding with new update" in m)
    assert cluster_deploy.REAP_CLEARED_QUALIFIER in line
    assert "reaped" not in line
    assert backend.killed == ["ava-updater"]  # attempted — and it reported failure


def test_the_watchdog_reap_log_does_not_claim_a_kill_on_the_recheck_path(
    logs: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The second emitter of the same sentence, driven down the same ambiguous path.

    Both call sites interpolate `REAP_CLEARED_QUALIFIER` instead of spelling the
    qualifier out, so "the two agree" is a property of the source rather than of
    whoever edits one of them next — and these two tests are what says each call site
    still reads it. A call site that goes back to its own wording keeps passing its own
    assertion and fails this one.
    """
    backend = _Backend(kills=False)
    _use(monkeypatch, backend)
    _lease(monkeypatch, expires_in_s=-60.0)
    # Alive for the round's liveness check, gone by the recheck after the kill.
    answers = iter([True, False])
    monkeypatch.setattr(cluster_session, "_has_orchestration_session", lambda _name: next(answers))  # pyright: ignore[reportUnknownArgumentType]

    with caplog.at_level("WARNING"):
        result = StalledUpdaterController().reconcile("agent-runner")

    assert result.acted is True
    # Selected by logger, not by wording: the record under test is "the one this call
    # site emitted", and a phrase match would make a reworded line look like a missing
    # one and report the drift as a StopIteration instead of the assertion below.
    line = next(
        r.getMessage() for r in caplog.records if r.name == "ops.controllers.stalled_updater"
    )
    assert cluster_deploy.REAP_CLEARED_QUALIFIER in line
    assert "reaped" not in line
    assert backend.killed == ["ava-updater"]  # attempted — and it reported failure


def test_an_unreadable_liveness_recheck_keeps_the_failure(
    logs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cannot tell whether it survived -> keep the error. A false "cleared" is the
    expensive direction: it spawns a second updater over a live one, which is the
    2026-05-25 incident (two `ava stop`s killing each other's sessions)."""

    def _boom(_name: str) -> bool:
        raise RuntimeError("session backend unreachable")

    backend = _Backend(kills=False)
    _use(monkeypatch, backend)
    monkeypatch.setattr(cluster_session, "_has_orchestration_session", _boom)
    _lease(monkeypatch, expires_in_s=-60.0)

    assert cluster_mod._reap_stalled_updater("ava-updater") is False


def test_no_lease_at_all_is_not_a_reap(logs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing to measure: leave the session alone and let the caller refuse with its
    `ClusterUpdateInProgress` (which names the manual kill)."""
    backend = _Backend()
    _use(monkeypatch, backend)

    assert cluster_mod._updater_hung("ava-updater") is False
    assert backend.killed == []


# ─── the scheduled half: the reap runs on the watchdog's own round ────────────
#
# `spawn_update`'s reap only fires when a new update reaches this host, and nothing
# reaches it: a live `ava-updater` makes `current_orchestration()` answer "update",
# which `ops.deploy_window` signal 2 reports to the gateway, which refuses the whole
# next rollout — so Phase B never fans out and the reap is never called. One corpse
# on one runner deadlocks the cluster's deploy path and that runner's own pin/code
# self-heal at the same time.


def test_the_round_reaps_a_session_whose_lease_expired(
    logs: Path, alive: list[bool], monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _Backend()
    _use(monkeypatch, backend)
    _lease(monkeypatch, expires_in_s=-60.0)

    assert cluster_mod.reap_stalled_updater_if_hung() is True
    assert backend.killed == ["ava-updater"]


def test_the_round_leaves_a_working_updater_alone(
    logs: Path, alive: list[bool], monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _Backend()
    _use(monkeypatch, backend)
    _lease(monkeypatch, expires_in_s=600.0)

    assert cluster_mod.reap_stalled_updater_if_hung() is False
    assert backend.killed == []


def test_no_session_is_not_a_reap_however_old_the_log_is(
    logs: Path, alive: list[bool], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The steady state of every host that has ever updated: the updater lease
    expires within the hour if nothing renews it. The lease alone therefore says
    nothing — the live session is the half that makes it a hung session rather
    than a finished one, and a round that skipped it would log a kill every 60 s
    for a session that is not there."""
    alive[0] = False
    backend = _Backend()
    _use(monkeypatch, backend)
    _lease(monkeypatch, expires_in_s=-60.0)

    assert cluster_mod.reap_stalled_updater_if_hung() is False
    assert backend.killed == []


def test_a_broken_session_probe_does_not_escape_the_round(
    logs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The manager wraps no controller in a try/except — an exception out of one
    reconcile kills the watchdog process — so this swallows its own."""

    def _boom(_name: str) -> bool:
        raise RuntimeError("session backend unreachable")

    monkeypatch.setattr(cluster_session, "_has_orchestration_session", _boom)
    assert cluster_mod.reap_stalled_updater_if_hung() is False


def test_the_controller_acts_without_blocking_the_round(
    logs: Path, alive: list[bool], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Killing the corpse takes nothing down, so the round must go on to the
    controllers that were deferring to it — pause, pin and code all have work to do
    on a host whose updater just died."""
    backend = _Backend()
    _use(monkeypatch, backend)
    _lease(monkeypatch, expires_in_s=-60.0)

    result = StalledUpdaterController().reconcile("agent-runner")

    assert result.acted is True
    assert result.blocks is BlockScope.NONE
    assert backend.killed == ["ava-updater"]


def test_the_gateway_watchdog_does_not_reap(monkeypatch: pytest.MonkeyPatch) -> None:
    """A gateway-only unit runs no ops daemon, so it can hold no `ava-updater`; on a
    single box the co-located agent-runner watchdog already owns the session, and a
    second killer would only race it. The role check is ahead of the session probe, so
    the gateway leg costs nothing."""

    def _never(_name: str) -> bool:
        raise AssertionError("the gateway watchdog must not probe for an updater session")

    monkeypatch.setattr(cluster_session, "_has_orchestration_session", _never)

    result = StalledUpdaterController().reconcile("gateway")

    assert result.acted is False
    assert result.blocks is BlockScope.NONE


async def test_the_reap_happens_before_the_pause_gate_ends_the_round(
    logs: Path, alive: list[bool], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The ordering that makes the whole thing work. `spawn_update` pauses the host
    before it spawns, so a hung updater is *always* found on a paused host — and
    `PauseController` blocks with `BlockScope.ALL`, which short-circuits every
    controller behind it. A reaper anywhere after `pause` in the default list would
    therefore never run in the one situation it exists for.
    """
    from datetime import UTC, datetime, timedelta

    from ops.manager import ControllerManager
    from shared.host_deploy_state import UPDATER_LEASE_TTL_S, HostDeployState

    now = datetime.now(UTC)
    expired = now - timedelta(seconds=60)
    state = HostDeployState(
        machine="laptop-host",
        posture="paused",
        updated_at=now,  # fresh: paused, and not yet stranded
        updater_lease_expires_at=expired,
        # This pause opened before the lease was armed, which is the only ordering a
        # real hung updater can have — the pause comes first, then the updater's own
        # touch (`HostDeployState.updater_expired`).
        paused_at=expired - timedelta(seconds=UPDATER_LEASE_TTL_S + 1),
    )
    monkeypatch.setattr(
        "shared.host_deploy_state.read",
        lambda machine=None, **_kw: state,  # noqa: ARG005  # pyright: ignore[reportUnknownArgumentType]
    )
    backend = _Backend()
    _use(monkeypatch, backend)

    manager = ControllerManager()  # the DEFAULT list — the order under test
    blocks = await manager.reconcile("agent-runner")

    assert backend.killed == ["ava-updater"]  # reaped despite the pause behind it
    assert blocks is BlockScope.ALL  # and the pause still owns the round


def test_the_three_timeouts_are_one_number() -> None:
    """The family's point. The Phase-B poll, the settle-hold TTL and this reaper all
    answer "has this host stopped making progress?", and the smallest of the three used
    to decide when the deploy lease stopped protecting the deploy: a POSIX-era 120 s
    poll against two 900 s siblings. Two clocks that disagree about that are two
    chances to declare a working host dead, or a dead one working."""
    import cli.commands.update as update_mod
    from shared.cluster_lock import SETTLE_TTL_S
    from shared.deploy_timing import NO_PROGRESS_TIMEOUT_S

    assert cluster_mod._UPDATER_STALL_TIMEOUT_S == NO_PROGRESS_TIMEOUT_S
    assert SETTLE_TTL_S == NO_PROGRESS_TIMEOUT_S
    assert update_mod._POLL_TIMEOUT_S == NO_PROGRESS_TIMEOUT_S


def test_the_lease_ttl_is_many_renewal_intervals_wide() -> None:
    """`LOCK_TTL_S` is the crash-reclaim bound, and renewal is what keeps a live rollout
    inside it — so one missed round (a slow DB, a dropped connection) must never be
    enough to lapse the lease mid-deploy."""
    from shared.cluster_lock import LOCK_TTL_S
    from shared.deploy_timing import LEASE_RENEW_INTERVAL_S

    assert LOCK_TTL_S >= 10 * LEASE_RENEW_INTERVAL_S
