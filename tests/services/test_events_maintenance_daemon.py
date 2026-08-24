"""`services.events_maintenance.daemon` loop-hardening + gating behaviors.

The runtime guards the maintenance daemon needs and that are easy to regress:
  - each concurrent loop owns a progress deadline that sibling loops cannot mask;
  - blocking passes beat only after completion and permanently wedge on timeout;
  - a failed pass still waits a full interval before retrying, so a transient
    DB error does not become a tight hot-loop against Postgres;
  - the events/checkpoint split (task #1257): the hourly pass skips the
    events-archive slices when `AVA_EVENTS_MAINTENANCE_ENABLED` is off but
    ALWAYS runs the Rule B reaper + blob vacuum, and Rule A rides its own fast
    loop again (both were collateral of the #1197 cutover).

All driven directly (no DB): `_run_maintenance` / `_maintenance_with_liveness`
are monkeypatched, so these are pure asyncio-loop tests.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, cast

import pytest
from psycopg_pool import ConnectionPool

from services.events_maintenance import daemon
from shared.config.daemon import DaemonSettings
from shared.daemon_health import LivenessGroup, LoopProgress

# The pool is never touched — `_run_maintenance` / `_maintenance_with_liveness` are faked.
_FAKE_POOL: Any = object()


class _FakePool:
    """A pool whose `connection()` returns a working no-op context manager — the
    maintenance-pass tests fake every slice, but `_run_maintenance` opens a
    `with pool.connection()` wrapper around each one."""

    class _Conn:
        def __enter__(self) -> _FakePool._Conn:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    def connection(self) -> _FakePool._Conn:
        return self._Conn()


def test_wedged_pass_fails_without_beating_in_flight(monkeypatch: pytest.MonkeyPatch) -> None:
    """A worker that exceeds its hard deadline wedges; elapsed time never earns a beat."""
    progress = LoopProgress("dispatch", timeout_s=0.01)
    beats = 0
    original_beat = progress.beat

    def counting_beat() -> None:
        nonlocal beats
        beats += 1
        original_beat()

    monkeypatch.setattr(progress, "beat", counting_beat)

    with pytest.raises(daemon.WedgedPassError, match=r"dispatch.*hard deadline"):
        asyncio.run(
            daemon._maintenance_with_liveness(
                _FAKE_POOL,
                progress,
                run=lambda _pool: time.sleep(0.05),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
            )
        )

    assert beats == 0
    assert not progress.is_alive()
    assert progress.snapshot()["wedged"] is True


def test_completed_pass_beats_once_after_worker_finishes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bounded worker completion advances progress exactly once, after it returns."""
    progress = LoopProgress("dispatch", timeout_s=1.0)
    finished_at = 0.0
    beat_at: list[float] = []
    original_beat = progress.beat

    def run(_pool: object) -> None:
        nonlocal finished_at
        finished_at = time.monotonic()

    def counting_beat() -> None:
        beat_at.append(time.monotonic())
        original_beat()

    monkeypatch.setattr(progress, "beat", counting_beat)
    asyncio.run(daemon._maintenance_with_liveness(_FAKE_POOL, progress, run=run))

    assert len(beat_at) == 1
    assert beat_at[0] >= finished_at


def test_failed_rollup_still_waits_before_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """A maintenance pass that raises a transient (non-ProgrammingError) exception
    beats on completion, records the error, then waits before re-running."""
    failed_at = 0.0

    def boom(*_args: object) -> None:
        nonlocal failed_at
        failed_at = time.monotonic()
        raise RuntimeError("transient db blip")

    slept: list[float] = []

    async def fake_sleep(_progress: LoopProgress, total_s: float) -> None:
        slept.append(total_s)
        raise asyncio.CancelledError  # break the otherwise-infinite loop after one sleep

    monkeypatch.setattr(daemon, "_run_maintenance", boom)
    monkeypatch.setattr(daemon, "_sleep_with_liveness", fake_sleep)

    progress = LoopProgress("dispatch", timeout_s=5.0)
    beat_at: list[float] = []
    original_beat = progress.beat

    def counting_beat() -> None:
        beat_at.append(time.monotonic())
        original_beat()

    monkeypatch.setattr(progress, "beat", counting_beat)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(daemon._dispatch_loop(_FAKE_POOL, progress))  # pool unused

    assert len(beat_at) == 1
    assert beat_at[0] >= failed_at
    assert progress.snapshot()["last_error"]["message"] == "transient db blip"  # pyright: ignore[reportIndexIssue]
    assert slept == [daemon.settings.daemon.events_maintenance_interval_seconds]


def test_wedged_dispatch_parks_without_entering_retry_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timed-out worker may still hold a connection, so its loop parks permanently."""

    async def wedge(_pool: object, progress: LoopProgress) -> None:
        progress.fail("dispatch exceeded hard deadline")
        raise daemon.WedgedPassError("dispatch exceeded hard deadline")

    parked: list[float] = []

    async def fake_park(total_s: float) -> None:
        parked.append(total_s)
        raise asyncio.CancelledError

    async def forbidden_retry_sleep(_progress: LoopProgress, _total_s: float) -> None:
        pytest.fail("wedged loop entered the normal retry sleep")

    monkeypatch.setattr(daemon, "_maintenance_with_liveness", wedge)
    monkeypatch.setattr(daemon.asyncio, "sleep", fake_park)
    monkeypatch.setattr(daemon, "_sleep_with_liveness", forbidden_retry_sleep)

    progress = LoopProgress("dispatch", timeout_s=1.0)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(daemon._dispatch_loop(_FAKE_POOL, progress))

    assert parked == [3600]
    assert not progress.is_alive()


# ─── the events/checkpoint split (design-regression fix, task #1257) ──────────────


class _CallRecorder:
    """Call counter for one maintenance slice, returning a harmless fake result."""

    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls = 0
        self.kwargs: list[dict[str, object]] = []

    def __call__(self, *args: object, **kwargs: object) -> Any:
        self.calls += 1
        self.kwargs.append(kwargs)
        return self.result


def _instrument_maintenance_slices(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, _CallRecorder]:
    """Replace every slice `_run_maintenance` can invoke with a recorder. The
    fake results are the shapes the pass's logging branches read (empty/zero so
    nothing logs)."""
    from types import SimpleNamespace

    empty = SimpleNamespace(dropped=(), pruned=(), deleted=(), summary=lambda: "")
    rec = {
        "partitions": _CallRecorder([]),
        "retention": _CallRecorder(empty),
        "table_retention": _CallRecorder(empty),
        "rollup": _CallRecorder(
            SimpleNamespace(start_day=None, end_day=None, metrics_rows=0, tokens_rows=0)
        ),
        "replay": _CallRecorder(
            SimpleNamespace(days_replayed=[], days_failed=[], metrics_rows=0, tokens_rows=0)
        ),
        "reindex": _CallRecorder(empty),
        "reap": _CallRecorder(SimpleNamespace(agents=0, checkpoints=0, writes=0, blobs=0)),
        "vacuum": _CallRecorder(SimpleNamespace(ran=False, summary=lambda: "")),
    }
    monkeypatch.setattr(daemon, "ensure_month_partitions", rec["partitions"])
    monkeypatch.setattr(daemon, "apply_retention", rec["retention"])
    monkeypatch.setattr(daemon, "apply_table_retention", rec["table_retention"])
    monkeypatch.setattr(daemon, "compute_rollup", rec["rollup"])
    monkeypatch.setattr(daemon, "replay_gap_days", rec["replay"])
    monkeypatch.setattr(daemon, "run_governance_pass", rec["reindex"])
    monkeypatch.setattr(daemon, "reap_stale_checkpoints", rec["reap"])
    monkeypatch.setattr(daemon, "run_blob_vacuum", rec["vacuum"])
    return rec


_EVENTS_SLICES = ("partitions", "retention", "table_retention", "reindex")


def test_maintenance_pass_reaps_when_events_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """The design-regression lock (task #1257): with
    `events_maintenance_enabled=False` — the default since the LGTM cutover —
    the hourly pass skips every events-archive slice but still runs the
    cost-ledger rollup, the Rule B checkpoint reaper and the blob vacuum. The
    rollup must not sit behind the archive flag: Loki only retains 168h, so a
    cluster without the flag would silently stop writing the durable cost
    ledger (and the reaper regression before it grew checkpoint_blobs
    ~150MB/h unbounded)."""
    monkeypatch.setattr(daemon.settings.daemon, "events_maintenance_enabled", False)
    rec = _instrument_maintenance_slices(monkeypatch)
    progress = LoopProgress("dispatch", timeout_s=60.0)
    beats = 0
    original_beat = progress.beat

    def counting_beat() -> None:
        nonlocal beats
        beats += 1
        original_beat()

    monkeypatch.setattr(progress, "beat", counting_beat)

    daemon._run_maintenance(cast(ConnectionPool, _FakePool()), progress)  # every slice faked

    assert rec["rollup"].calls == 1
    assert set(rec["rollup"].kwargs[0]) == {"now_utc"}
    assert rec["replay"].calls == 1
    assert rec["reap"].calls == 1
    assert rec["vacuum"].calls == 1
    assert beats == 3
    assert progress.snapshot()["last_success_at"] is not None
    for name in _EVENTS_SLICES:
        assert rec[name].calls == 0, f"events slice {name} ran with the flag off"


def test_maintenance_pass_runs_events_slices_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flag on (clusters that still maintain the archive) restores the full
    pass — every events slice plus the unconditional rollup, reaper and
    vacuum."""
    monkeypatch.setattr(daemon.settings.daemon, "events_maintenance_enabled", True)
    rec = _instrument_maintenance_slices(monkeypatch)
    progress = LoopProgress("dispatch", timeout_s=60.0)
    beats = 0
    original_beat = progress.beat

    def counting_beat() -> None:
        nonlocal beats
        beats += 1
        original_beat()

    monkeypatch.setattr(progress, "beat", counting_beat)

    daemon._run_maintenance(cast(ConnectionPool, _FakePool()), progress)  # every slice faked

    for name in (*_EVENTS_SLICES, "rollup", "replay", "reap", "vacuum"):
        assert rec[name].calls == 1, name
    assert beats == 7
    assert progress.snapshot()["last_success_at"] is not None


def test_checkpoint_trim_loop_runs_rule_a(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule A (overgrown live threads) rides its own fast loop again: #1197
    removed the ops-rollup fast loop it shared, silently leaving
    `trim_overgrown_threads` dead code. The loop runs unconditionally — it must
    not be coupled to the events flag (same regression class as the reaper)."""
    ran: list[object] = []

    def fake_trim(pool: object, progress: LoopProgress) -> None:
        ran.append(pool)
        progress.beat()
        progress.mark_success()

    async def fake_sleep(_progress: LoopProgress, total_s: float) -> None:
        raise asyncio.CancelledError  # break the otherwise-infinite loop after one run

    monkeypatch.setattr(daemon, "_run_checkpoint_trim", fake_trim)
    monkeypatch.setattr(daemon, "_sleep_with_liveness", fake_sleep)

    progress = LoopProgress("trim", timeout_s=5.0)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(daemon._checkpoint_trim_loop(_FAKE_POOL, progress))  # pool unused
    assert ran == [_FAKE_POOL]
    assert progress.snapshot()["last_success_at"] is not None
    # The fast-loop cadence is its own constant, not the hourly maintenance interval.
    hourly = daemon.settings.daemon.events_maintenance_interval_seconds
    assert hourly > daemon._CHECKPOINT_TRIM_INTERVAL_S


def test_deadline_settings_defaults_and_env_aliases() -> None:
    defaults = DaemonSettings()
    assert defaults.events_maintenance_pass_deadline_s == 1500.0
    assert defaults.events_maintenance_trim_deadline_s == 300.0
    assert defaults.events_maintenance_resolution_deadline_s == 600.0

    configured = DaemonSettings.model_validate(
        {
            "AVA_EVENTS_MAINTENANCE_PASS_DEADLINE_S": "15.5",
            "AVA_EVENTS_MAINTENANCE_TRIM_DEADLINE_S": "25",
            "AVA_EVENTS_MAINTENANCE_RESOLUTION_DEADLINE_S": "35.5",
        }
    )
    assert configured.events_maintenance_pass_deadline_s == 15.5
    assert configured.events_maintenance_trim_deadline_s == 25.0
    assert configured.events_maintenance_resolution_deadline_s == 35.5


def test_run_gives_each_loop_its_own_progress_tracker(monkeypatch: pytest.MonkeyPatch) -> None:
    """The three concurrent loops cannot share a progress stamp at the run boundary."""

    class _RunPool:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    pool = _RunPool()
    health = object()
    health_liveness: list[object] = []
    received: dict[str, LoopProgress] = {}

    async def fake_start(_name: str, *, liveness: object) -> object:
        health_liveness.append(liveness)
        return health

    async def fake_stop(server: object) -> None:
        assert server is health

    async def dispatch(_pool: object, progress: LoopProgress) -> None:
        received["dispatch"] = progress

    async def trim(_pool: object, progress: LoopProgress) -> None:
        received["trim"] = progress

    async def resolution(_pool: object, progress: LoopProgress) -> None:
        received["resolution"] = progress

    def fake_health_port(_name: str) -> int:
        return 8109

    monkeypatch.setattr(daemon, "_is_running", lambda: False)
    monkeypatch.setattr(daemon, "_write_pidfile", lambda: None)
    monkeypatch.setattr(daemon, "_remove_pidfile", lambda: None)
    monkeypatch.setattr(daemon, "start_health_server", fake_start)
    monkeypatch.setattr(daemon, "stop_health_server", fake_stop)
    monkeypatch.setattr(daemon, "health_port", fake_health_port)
    monkeypatch.setattr(daemon.shared.db, "pool", lambda: pool)
    monkeypatch.setattr(daemon, "_dispatch_loop", dispatch)
    monkeypatch.setattr(daemon, "_checkpoint_trim_loop", trim)
    monkeypatch.setattr(daemon, "_resolution_loop", resolution)

    asyncio.run(daemon.run())

    assert pool.closed
    assert len(health_liveness) == 1
    assert isinstance(health_liveness[0], LivenessGroup)
    assert len({id(progress) for progress in received.values()}) == 3
    assert received["dispatch"].timeout_s == 1500.0
    assert received["trim"].timeout_s == 300.0
    assert received["resolution"].timeout_s == 600.0
