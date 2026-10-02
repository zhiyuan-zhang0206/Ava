"""The reaper's loop structure: the TaskGroup that owns the two loops, a crash
that ends the process, the durable cadence clocks, the sweep's slow phases, the
remote round, and the gateway no longer owning a reaper."""

from __future__ import annotations

import asyncio
import importlib.util
from collections.abc import Awaitable, Callable, Iterator
from typing import Any, cast
from unittest.mock import MagicMock

import psycopg
import pytest
from psycopg_pool import ConnectionPool

import base.db
import gateway.app
from base.config import settings
from base.daemon.loop_health import LivenessGroup, LoopProgress
from base.deploy.maintenance import admission
from services.ttl_reaper import cadence, daemon, remote, shells, sweep


@pytest.fixture
def pool() -> Iterator[ConnectionPool]:
    p = base.db.pool(max_size=4)
    try:
        yield p
    finally:
        p.close()


def _progress() -> LoopProgress:
    return LoopProgress("test", 60.0)


def _backdate(db: psycopg.Connection, kind: str, seconds: float) -> None:
    db.execute(
        "UPDATE maintenance_state SET last_run_at = clock_timestamp() - make_interval(secs => %s) "
        "WHERE kind = %s",
        (seconds, kind),
    )
    db.commit()


# --- the gateway no longer owns a reaper -------------------------------------


def test_the_gateway_owns_no_reaper() -> None:
    """The reaper is its own service: the gateway module is gone and its lifespan
    neither imports nor starts one, so no loop in the gateway can kill a shell."""
    assert importlib.util.find_spec("gateway.ttl_reaper") is None
    assert not hasattr(gateway.app, "ttl_reaper")


# --- loops end the process ---------------------------------------------------


def _park_or_crash(monkeypatch: pytest.MonkeyPatch, *, crashing: str, cancelled: list[str]) -> None:
    def stand_in(name: str):
        async def loop(*_args: object) -> None:
            if name == crashing:
                await asyncio.sleep(0.01)
                raise RuntimeError(f"{name} loop crashed")
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(name)
                raise

        return loop

    monkeypatch.setattr(daemon.sweep, "sweep_loop", stand_in("sweep"))
    monkeypatch.setattr(daemon.remote, "remote_loop", stand_in("remote"))


@pytest.mark.parametrize("crashing", ["sweep", "remote"])
async def test_a_crashing_loop_cancels_its_sibling_and_ends_the_service(
    pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch, crashing: str
) -> None:
    cancelled: list[str] = []
    _park_or_crash(monkeypatch, crashing=crashing, cancelled=cancelled)

    with pytest.raises(ExceptionGroup) as raised:
        await daemon._run_loops(pool, LivenessGroup())

    assert [str(exc) for exc in raised.value.exceptions] == [f"{crashing} loop crashed"]
    assert cancelled == [({"sweep", "remote"} - {crashing}).pop()]


async def test_each_loop_reports_its_own_progress(
    pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    liveness = LivenessGroup()
    _park_or_crash(monkeypatch, crashing="sweep", cancelled=[])

    with pytest.raises(ExceptionGroup):
        await daemon._run_loops(pool, liveness)

    assert set(liveness.snapshot()) == {"sweep", "remote"}


async def test_a_crash_leaves_run_after_releasing_its_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exception reaches `main` (exit code 1, supervisor restarts the unit),
    and the pool, the health server and the pidfile are released on the way."""
    released: list[str] = []

    class _Pool:
        def close(self) -> None:
            released.append("pool")

    async def start_health(_name: str, **_kw: object) -> str:
        return "health"

    async def stop_health(_server: object) -> None:
        released.append("health")

    async def crashing_loops(_pool: object, _liveness: object) -> None:
        raise ExceptionGroup("loops", [RuntimeError("sweep loop crashed")])

    def acquire(_path: object, _module: str) -> bool:
        return True

    def remove(_path: object) -> None:
        released.append("pidfile")

    def make_pool(**_kw: object) -> _Pool:
        return _Pool()

    monkeypatch.setattr(daemon, "acquire_pidfile", acquire)
    monkeypatch.setattr(daemon, "remove_pidfile", remove)
    monkeypatch.setattr(daemon, "start_health_server", start_health)
    monkeypatch.setattr(daemon, "stop_health_server", stop_health)
    monkeypatch.setattr(daemon.base.db, "pool", make_pool)
    monkeypatch.setattr(daemon, "_run_loops", crashing_loops)

    with pytest.raises(ExceptionGroup):
        await daemon.run()

    assert sorted(released) == ["health", "pidfile", "pool"]


# --- durable cadence clocks --------------------------------------------------


def test_a_phase_is_claimed_once_per_interval(
    db_conn: psycopg.Connection, pool: ConnectionPool
) -> None:
    assert cadence.claim_due(pool, cadence.TORN_POINTER_SCAN, 3600.0) is True
    assert cadence.claim_due(pool, cadence.TORN_POINTER_SCAN, 3600.0) is False
    # Each phase keeps its own clock.
    assert cadence.claim_due(pool, cadence.ABSENT_FENCE_SETTLE, 3600.0) is True

    _backdate(db_conn, cadence.TORN_POINTER_SCAN, 3601.0)
    assert cadence.claim_due(pool, cadence.TORN_POINTER_SCAN, 3600.0) is True
    assert cadence.claim_due(pool, cadence.TORN_POINTER_SCAN, 3600.0) is False


def test_a_claim_survives_a_restart(db_conn: psycopg.Connection, pool: ConnectionPool) -> None:
    """The clock is a row, not process state: a fresh pool (a restarted service)
    reads the stamp the previous one wrote."""
    assert cadence.claim_due(pool, cadence.FIRE_LOG_PRUNE, 86400.0) is True
    restarted = base.db.pool(max_size=2)
    try:
        assert cadence.claim_due(restarted, cadence.FIRE_LOG_PRUNE, 86400.0) is False
    finally:
        restarted.close()
    row = db_conn.execute("SELECT count(*) FROM maintenance_state").fetchone()
    assert row is not None and row[0] == 1


# --- the sweep's slow phases -------------------------------------------------


def _count_slow_phases(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    calls = {"prune": 0, "torn": 0, "settle": 0}

    def prune(_pool: object) -> int:
        calls["prune"] += 1
        return 0

    def torn(_pool: object) -> int:
        calls["torn"] += 1
        return 0

    def settle(_pool: object, *, batch: int) -> list[int]:
        calls["settle"] += 1
        return []

    monkeypatch.setattr(sweep, "_prune_schedule_fire_log_blocking", prune)
    monkeypatch.setattr(sweep, "_scan_torn_lifecycle_pointers_blocking", torn)
    monkeypatch.setattr(sweep, "settle_absent_machine_fences", settle)
    return calls


async def test_slow_phases_run_once_per_cadence_not_once_per_round(
    db_conn: psycopg.Connection, pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.daemon, "schedule_fire_log_cleanup_interval_seconds", 86400.0)
    calls = _count_slow_phases(monkeypatch)

    await sweep.sweep_round(pool, _progress())
    await sweep.sweep_round(pool, _progress())
    assert calls == {"prune": 1, "torn": 1, "settle": 1}

    # Two hours on: the hourly phases are due again, the daily prune is not.
    _backdate(db_conn, cadence.TORN_POINTER_SCAN, 7200.0)
    _backdate(db_conn, cadence.ABSENT_FENCE_SETTLE, 7200.0)
    _backdate(db_conn, cadence.FIRE_LOG_PRUNE, 7200.0)
    await sweep.sweep_round(pool, _progress())
    assert calls == {"prune": 1, "torn": 2, "settle": 2}


async def test_a_restarted_sweep_does_not_rerun_phases_it_already_ran(
    pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _count_slow_phases(monkeypatch)
    await sweep.sweep_round(pool, _progress())
    assert calls == {"prune": 1, "torn": 1, "settle": 1}

    # A new process: nothing in memory, the clocks are the table's.
    await sweep.sweep_round(pool, _progress())
    assert calls == {"prune": 1, "torn": 1, "settle": 1}


# --- the remote round --------------------------------------------------------


async def test_the_remote_round_reaps_shells_then_redelivers_work_failures(
    pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []

    async def reap(_pool: object, _progress: object) -> list[tuple[int, int]]:
        order.append("shells")
        return []

    async def reconcile(_pool: object, on_event: Any = None) -> int:
        order.append("work_failures")
        assert on_event is not None
        return 0

    monkeypatch.setattr(remote.shells, "reap_expired_shells", reap)
    monkeypatch.setattr(remote.work_failed_router, "reconcile_stale_work_failures", reconcile)

    await remote.remote_round(pool, _progress())

    assert order == ["shells", "work_failures"]


# --- shell reclaim is concurrent across machines, serial within one ----------


def _rows(*placements: tuple[str, int]) -> list[dict[str, object]]:
    return [
        {
            "agent_id": 1,
            "session_id": session_id,
            "machine": machine,
            "expires_at": None,
            "created_at": None,
        }
        for machine, session_id in placements
    ]


async def test_machines_are_reclaimed_concurrently_and_one_machines_rows_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow machine holds up only its own rows: while machine `slow` is wedged
    in a dispatch, machine `fast` finishes both of its rows."""
    release = asyncio.Event()
    started: list[tuple[str, int]] = []
    finished: list[tuple[str, int]] = []

    async def reclaim(_pool: object, machine: str, row: dict[str, Any]) -> tuple[int, int] | None:
        started.append((machine, row["session_id"]))
        if machine == "slow":
            await release.wait()
        finished.append((machine, row["session_id"]))
        return 1, row["session_id"]

    def expired(_pool: object) -> list[dict[str, object]]:
        return _rows(("slow", 1), ("fast", 2), ("slow", 3), ("fast", 4))

    monkeypatch.setattr(shells, "_expired_shell_rows_blocking", expired)
    monkeypatch.setattr(shells, "_reclaim_row", reclaim)

    faked_pool = cast(ConnectionPool, None)  # every pool consumer above is faked
    task = asyncio.create_task(shells.reap_expired_shells(faked_pool, _progress()))
    for _ in range(100):
        if [m for m, _ in finished].count("fast") == 2:
            break
        await asyncio.sleep(0.01)
    assert finished == [("fast", 2), ("fast", 4)]
    assert started == [("slow", 1), ("fast", 2), ("fast", 4)]  # slow's 2nd row waits its turn

    release.set()
    reaped = await task
    assert sorted(reaped) == [(1, 1), (1, 2), (1, 3), (1, 4)]
    assert [s for m, s in started if m == "slow"] == [1, 3]


async def test_a_dispatch_past_its_deadline_defers_the_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dispatch that outlives the deadline the RPC client's own budget sizes is
    cut and the row left for the next round, never wedging the loop."""

    async def hang(*_a: object, **_kw: object) -> dict[str, object]:
        await asyncio.Event().wait()
        return {}

    monkeypatch.setattr(shells.cluster_rpc, "dispatch_to_machine", hang)
    monkeypatch.setattr(shells, "dispatch_deadline_s", lambda: 0.05)

    assert await shells._dispatch_shell_kill("macmini", 1, 2) is None


def test_the_dispatch_deadline_covers_the_clients_full_retry_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings.gateway, "cluster_rpc_max_retries", 3)
    monkeypatch.setattr(settings.gateway, "cluster_rpc_timeout_seconds", 30.0)

    # Four attempts at the reaper's own 5 s attempt budget, plus the client's backoff.
    assert shells.dispatch_deadline_s() >= 4 * 5.0
    assert shells.dispatch_deadline_s() < 4 * 30.0


# --- the stop window ---------------------------------------------------------


@pytest.mark.parametrize("loop", [sweep.sweep_loop, remote.remote_loop])
@pytest.mark.parametrize("quiesced", [True, False])
async def test_a_quiesced_unit_borrows_no_connection(
    monkeypatch: pytest.MonkeyPatch,
    loop: Callable[[ConnectionPool, LoopProgress], Awaitable[None]],
    quiesced: bool,
) -> None:
    monkeypatch.setattr(admission, "quiesced", lambda: quiesced)
    pool = MagicMock()
    task = asyncio.create_task(loop(cast("ConnectionPool", pool), _progress()))
    try:
        await asyncio.sleep(0.2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert bool(pool.mock_calls) is (not quiesced)
