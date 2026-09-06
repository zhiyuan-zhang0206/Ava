"""`services.events_maintenance.daemon` loop-hardening + gating behaviors.

The runtime guards the maintenance daemon needs and that are easy to regress:
  - each concurrent loop owns a progress deadline that sibling loops cannot mask;
  - blocking passes beat only after completion and permanently wedge on timeout;
  - a failed pass still waits a full interval before retrying, so a transient
    DB error does not become a tight hot-loop against Postgres;
  - the hourly pass ALWAYS runs the rollup + blob vacuum, while uniform
    checkpoint pruning rides its own unconditional fast loop.

All driven directly (no DB): `_run_maintenance` / `_maintenance_with_liveness` are
monkeypatched, so these are pure asyncio-loop tests.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from types import SimpleNamespace
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

    class _Cursor:
        def __enter__(self) -> _FakePool._Cursor:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    class _Conn:
        def __enter__(self) -> _FakePool._Conn:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def cursor(self) -> _FakePool._Cursor:
            return _FakePool._Cursor()

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
                run=lambda _pool: time.sleep(0.05),
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
    """A timed-out worker parks by ending its loop without entering either sleep path."""

    async def wedge(_pool: object, progress: LoopProgress) -> None:
        progress.fail("dispatch exceeded hard deadline")
        raise daemon.WedgedPassError("dispatch exceeded hard deadline")

    async def forbidden_park(_total_s: float) -> None:
        pytest.fail("wedged loop entered an explicit park sleep")

    async def forbidden_retry_sleep(_progress: LoopProgress, _total_s: float) -> None:
        pytest.fail("wedged loop entered the normal retry sleep")

    monkeypatch.setattr(daemon, "_maintenance_with_liveness", wedge)
    monkeypatch.setattr(daemon.asyncio, "sleep", forbidden_park)
    monkeypatch.setattr(daemon, "_sleep_with_liveness", forbidden_retry_sleep)

    progress = LoopProgress("dispatch", timeout_s=1.0)
    asyncio.run(daemon._dispatch_loop(_FAKE_POOL, progress))

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
    rec = {
        "rollup": _CallRecorder(
            SimpleNamespace(start_day=None, end_day=None, metrics_rows=0, tokens_rows=0)
        ),
        "replay": _CallRecorder(
            SimpleNamespace(days_replayed=[], days_failed=[], metrics_rows=0, tokens_rows=0)
        ),
        "vacuum": _CallRecorder(SimpleNamespace(ran=False, summary=lambda: "")),
        "emit_sizes": _CallRecorder(None),
    }
    monkeypatch.setattr(daemon, "compute_rollup", rec["rollup"])
    monkeypatch.setattr(daemon, "replay_gap_days", rec["replay"])
    monkeypatch.setattr(daemon, "run_blob_vacuum", rec["vacuum"])
    monkeypatch.setattr(daemon, "emit_checkpoint_table_sizes", rec["emit_sizes"])
    return rec


def test_maintenance_pass_runs_unconditional_slices(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hourly pass always runs the cost-ledger rollup, the JSONL replay,
    the size telemetry sample, and blob vacuum. Checkpoint pruning belongs only
    to the fast loop; the hourly pass must not run a second retention rule."""
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

    for name in ("rollup", "replay", "vacuum", "emit_sizes"):
        assert rec[name].calls == 1, name
    assert beats == 3
    assert progress.snapshot()["last_success_at"] is not None


def test_checkpoint_trim_pass_prunes_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    prune = _CallRecorder(SimpleNamespace(agents=0, checkpoints=0, writes=0, blobs=0))
    monkeypatch.setattr(daemon, "prune_threads", prune)
    progress = LoopProgress("trim", timeout_s=5.0)

    daemon._run_checkpoint_trim(cast(ConnectionPool, _FAKE_POOL), progress)

    assert prune.calls == 1
    assert progress.snapshot()["last_success_at"] is not None


def test_checkpoint_prune_loop_runs_on_fast_cadence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Uniform pruning is unconditional and independent of the hourly loop."""
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


def test_loop_components_report_fresh_loops_as_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 100.0
    monkeypatch.setattr(daemon.time, "monotonic", lambda: now)
    liveness = LivenessGroup()
    liveness.register("dispatch", timeout_s=15.0)
    liveness.register("trim", timeout_s=10.0)
    liveness.register("resolution", timeout_s=20.0)

    assert daemon._loop_components(liveness) == [
        {"name": "dispatch", "status": "ok", "progress": "idle"},
        {"name": "trim", "status": "ok", "progress": "idle"},
        {"name": "resolution", "status": "ok", "progress": "idle"},
    ]


def test_loop_components_report_wedged_loop_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(daemon.time, "monotonic", lambda: 100.0)
    liveness = LivenessGroup()
    dispatch = liveness.register("dispatch", timeout_s=15.0)
    dispatch.fail("dispatch pass exceeded hard deadline")

    assert daemon._loop_components(liveness) == [
        {
            "name": "dispatch",
            "status": "degraded",
            "last_error": "dispatch pass exceeded hard deadline",
            "progress": "wedged",
            "detail": "wedged: dispatch pass exceeded hard deadline",
        }
    ]


def test_loop_components_report_stale_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 100.0
    monkeypatch.setattr(daemon.time, "monotonic", lambda: now)
    liveness = LivenessGroup()
    liveness.register("trim", timeout_s=5.0)
    now = 106.0

    assert daemon._loop_components(liveness) == [
        {
            "name": "trim",
            "status": "degraded",
            "progress": "idle",
            "detail": "no progress for 6s",
        }
    ]


def test_loop_components_convert_success_iso_to_epoch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(daemon.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(daemon.time, "time", lambda: 1_787_659_212.5)
    liveness = LivenessGroup()
    dispatch = liveness.register("dispatch", timeout_s=15.0)
    monkeypatch.setattr(dispatch, "_last_success_at", "2026-08-25T12:00:00+00:00")

    assert daemon._loop_components(liveness) == [
        {
            "name": "dispatch",
            "status": "ok",
            "last_success": 1_787_659_200.0,
            "age_s": 12.5,
            "progress": "idle",
        }
    ]


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
    health_components: list[Callable[[], list[dict[str, object]]]] = []
    received: dict[str, LoopProgress] = {}

    async def fake_start(_name: str, *, liveness: object, components: Any) -> object:
        health_liveness.append(liveness)
        health_components.append(components)
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
    assert len(health_components) == 1
    assert callable(health_components[0])
    assert [record["name"] for record in health_components[0]()] == [
        "dispatch",
        "trim",
        "resolution",
    ]
    assert len({id(progress) for progress in received.values()}) == 3
    assert received["dispatch"].timeout_s == 1500.0
    assert received["trim"].timeout_s == 300.0
    assert received["resolution"].timeout_s == 600.0
