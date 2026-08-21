"""`services.events_maintenance.daemon` loop-hardening + gating behaviors.

The runtime guards the maintenance daemon needs and that are easy to regress:
  - liveness is beaten *while* the (blocking) maintenance pass runs, so a long
    first-run backfill does not read as a wedged loop and get respawned mid-scan;
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
from shared.daemon_health import Liveness

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


def test_liveness_beaten_during_slow_rollup(monkeypatch: pytest.MonkeyPatch) -> None:
    """A maintenance pass longer than one beat step beats liveness several times
    while it runs (the old single `await to_thread` beat zero times during the scan)."""
    monkeypatch.setattr(daemon, "_LIVENESS_BEAT_STEP_S", 0.02)
    monkeypatch.setattr(daemon, "_run_maintenance", lambda _pool: time.sleep(0.2))  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]

    liveness = Liveness(timeout_s=5.0)
    beats = 0
    original_beat = liveness.beat

    def counting_beat() -> None:
        nonlocal beats
        beats += 1
        original_beat()

    monkeypatch.setattr(liveness, "beat", counting_beat)
    asyncio.run(
        daemon._maintenance_with_liveness(_FAKE_POOL, liveness)
    )  # pool unused (fake _run_maintenance)
    assert beats >= 3


def test_failed_rollup_still_waits_before_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """A maintenance pass that raises a transient (non-ProgrammingError) exception
    falls through to the inter-run sleep instead of immediately re-running."""

    async def boom(_pool: object, _liveness: Liveness) -> None:
        raise RuntimeError("transient db blip")

    slept: list[float] = []

    async def fake_sleep(_liveness: Liveness, total_s: float) -> None:
        slept.append(total_s)
        raise asyncio.CancelledError  # break the otherwise-infinite loop after one sleep

    monkeypatch.setattr(daemon, "_maintenance_with_liveness", boom)
    monkeypatch.setattr(daemon, "_sleep_with_liveness", fake_sleep)

    liveness = Liveness(timeout_s=5.0)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(daemon._dispatch_loop(_FAKE_POOL, liveness))  # pool unused
    # Exactly one sleep, of the full configured interval — the failure path waited.
    assert slept == [daemon.settings.daemon.events_maintenance_interval_seconds]


# ─── the events/checkpoint split (design-regression fix, task #1257) ──────────────


class _CallRecorder:
    """Call counter for one maintenance slice, returning a harmless fake result."""

    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls = 0

    def __call__(self, *args: object, **kwargs: object) -> Any:
        self.calls += 1
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
        "reindex": _CallRecorder(empty),
        "reap": _CallRecorder(SimpleNamespace(agents=0, checkpoints=0, writes=0, blobs=0)),
        "vacuum": _CallRecorder(SimpleNamespace(ran=False, summary=lambda: "")),
    }
    monkeypatch.setattr(daemon, "ensure_month_partitions", rec["partitions"])
    monkeypatch.setattr(daemon, "apply_retention", rec["retention"])
    monkeypatch.setattr(daemon, "apply_table_retention", rec["table_retention"])
    monkeypatch.setattr(daemon, "compute_rollup", rec["rollup"])
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

    daemon._run_maintenance(cast(ConnectionPool, _FakePool()))  # every slice faked

    assert rec["rollup"].calls == 1
    assert rec["reap"].calls == 1
    assert rec["vacuum"].calls == 1
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

    daemon._run_maintenance(cast(ConnectionPool, _FakePool()))  # every slice faked

    for name in (*_EVENTS_SLICES, "rollup", "reap", "vacuum"):
        assert rec[name].calls == 1, name


def test_checkpoint_trim_loop_runs_rule_a(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule A (overgrown live threads) rides its own fast loop again: #1197
    removed the ops-rollup fast loop it shared, silently leaving
    `trim_overgrown_threads` dead code. The loop runs unconditionally — it must
    not be coupled to the events flag (same regression class as the reaper)."""
    ran: list[object] = []

    def fake_trim(pool: object) -> None:
        ran.append(pool)

    async def fake_sleep(_liveness: Liveness, total_s: float) -> None:
        raise asyncio.CancelledError  # break the otherwise-infinite loop after one run

    monkeypatch.setattr(daemon, "_run_checkpoint_trim", fake_trim)
    monkeypatch.setattr(daemon, "_sleep_with_liveness", fake_sleep)

    liveness = Liveness(timeout_s=5.0)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(daemon._checkpoint_trim_loop(_FAKE_POOL, liveness))  # pool unused
    assert ran == [_FAKE_POOL]
    # The fast-loop cadence is its own constant, not the hourly maintenance interval.
    hourly = daemon.settings.daemon.events_maintenance_interval_seconds
    assert hourly > daemon._CHECKPOINT_TRIM_INTERVAL_S
