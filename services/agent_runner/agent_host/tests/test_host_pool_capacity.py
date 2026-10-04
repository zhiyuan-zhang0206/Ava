"""Real PgBouncer bounds for host pools, not a benchmark of running agents.

These tests deliberately use two PostgreSQL backends and many more client
leases. Short transactions release the server connection while a Python task
retains its client lease. Separate control pools reserve client capacity only;
they do not promise priority when every PgBouncer backend has a long transaction.
"""

import asyncio
import time
from collections.abc import Callable
from contextlib import AsyncExitStack

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool

from base.config import settings
from base.db import Database
from services.agent_runner.agent_host.pools import build_control_pool, build_shared_pool
from tests._containers import postgres
from tests.cli.test_pgbouncer_wire import (
    _admin_console_url,
    _pgbouncer_available,
    _pgbouncer_in_front,
)

pytestmark = pytest.mark.skipif(
    not _pgbouncer_available(), reason="pgbouncer not installed (brew/apt install pgbouncer)"
)

_BACKENDS = 2
_RUNNERS = 6
_BORROWERS_PER_RUNNER = 12
_REQUESTS = 1000
_LOCK_KEY = 816234


async def _pool_counts(admin: psycopg.AsyncConnection[DictRow], database: str) -> dict[str, int]:
    servers = await (await admin.execute("SHOW SERVERS")).fetchall()
    clients = await (await admin.execute("SHOW CLIENTS")).fetchall()
    return {
        "servers": sum(row["database"] == database for row in servers),
        "clients": sum(row["database"] == database for row in clients),
    }


async def _control_query(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        row = await (await conn.execute("SELECT 1")).fetchone()
        assert row == (1,)


async def _hold_workload_lease(
    workload: AsyncConnectionPool,
    value: int,
    *,
    acquired: asyncio.Queue[None],
    release: asyncio.Event,
    backend_ids: set[int],
) -> None:
    """Borrow one workload lease, report it, and keep it until `release`."""
    async with workload.connection() as conn:
        row = await (await conn.execute("SELECT %s::int, pg_backend_pid()", (value,))).fetchone()
        assert row is not None and row[0] == value
        backend_ids.add(row[1])
        acquired.put_nowait(None)
        await release.wait()


async def _observe_full_workload_pool(
    workload: AsyncConnectionPool,
    control: AsyncConnectionPool,
    admin: psycopg.AsyncConnection[DictRow],
    pooled: str,
    *,
    borrower_count: int,
    acquired: asyncio.Queue[None],
    backend_ids: set[int],
    tasks: asyncio.TaskGroup,
    record_property: Callable[[str, object], None],
) -> None:
    """With every lease held, one more workload request queues while control
    still reads, and PgBouncer's client/server counts stay bounded."""
    async with asyncio.timeout(15):
        for _ in range(borrower_count):
            await acquired.get()
        queued = tasks.create_task(_control_query(workload))
        while workload.get_stats()["requests_waiting"] == 0:
            await asyncio.sleep(0)
        assert workload.get_stats()["requests_waiting"] == 1
        assert not queued.done()
        # All workload clients remain borrowed. Their autocommit
        # queries released the two backends for this control read.
        await _control_query(control)
        assert not queued.done()
        counts = await _pool_counts(admin, str(conninfo_to_dict(pooled)["dbname"]))
        assert counts["clients"] >= borrower_count + 1
        assert 0 < counts["servers"] <= _BACKENDS
        assert 0 < len(backend_ids) <= _BACKENDS
        record_property("held_workload_leases", borrower_count)
        record_property("queued_workload_requests", 1)
        record_property("pgbouncer_clients", counts["clients"])
        record_property("pgbouncer_servers", counts["servers"])


async def _borrow_every_lease_then_release(
    workload: AsyncConnectionPool,
    control: AsyncConnectionPool,
    admin: psycopg.AsyncConnection[DictRow],
    pooled: str,
    record_property: Callable[[str, object], None],
) -> None:
    borrower_count = 64
    acquired: asyncio.Queue[None] = asyncio.Queue()
    release = asyncio.Event()
    backend_ids: set[int] = set()
    async with asyncio.TaskGroup() as tasks:
        for value in range(borrower_count):
            tasks.create_task(
                _hold_workload_lease(
                    workload, value, acquired=acquired, release=release, backend_ids=backend_ids
                )
            )
        try:
            await _observe_full_workload_pool(
                workload,
                control,
                admin,
                pooled,
                borrower_count=borrower_count,
                acquired=acquired,
                backend_ids=backend_ids,
                tasks=tasks,
                record_property=record_property,
            )
        finally:
            release.set()


async def test_more_than_twenty_workload_leases_share_bounded_backends(
    record_property: Callable[[str, object], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default 64 leases fit; the 65th waits without blocking control.

    The old 20-client workload pool cannot reach this borrower barrier.
    """
    with postgres() as direct, _pgbouncer_in_front(direct, pool_size=_BACKENDS) as pooled:
        # The host pools dial the cluster's access URL, which is this pooler.
        monkeypatch.setattr(settings.data_plane, "db_url", pooled)
        async with (
            build_shared_pool(Database.from_settings()) as workload,
            build_control_pool(Database.from_settings()) as control,
            await psycopg.AsyncConnection[DictRow].connect(
                _admin_console_url(pooled), autocommit=True, row_factory=dict_row
            ) as admin,
        ):
            await _borrow_every_lease_then_release(
                workload, control, admin, pooled, record_property
            )
            assert workload.get_stats()["requests_waiting"] == 0
            await _control_query(workload)


async def _cancel_blocked_transaction(
    pool: AsyncConnectionPool, observer: psycopg.AsyncConnection, request_id: int
) -> None:
    """Cancel an actual blocked SQL query after its earlier INSERT succeeded."""

    async def transaction() -> None:
        async with pool.connection() as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO host_pool_capacity_results VALUES (%s, %s)",
                (request_id, request_id * 7),
            )
            await conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))

    await observer.execute("SELECT pg_advisory_lock(%s)", (_LOCK_KEY,))
    pending = asyncio.create_task(transaction())
    try:
        async with asyncio.timeout(10):
            while True:
                row = await (
                    await observer.execute(
                        "SELECT EXISTS (SELECT 1 FROM pg_locks "
                        "WHERE locktype = 'advisory' AND NOT granted AND objid = %s)",
                        (_LOCK_KEY,),
                    )
                ).fetchone()
                assert row is not None
                if row[0]:
                    break
                if pending.done():
                    await pending
                    pytest.fail("transaction completed without waiting on the held SQL lock")
                await asyncio.sleep(0.01)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
    finally:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await observer.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_KEY,))
    await _control_query(pool)


async def _insert_requests_holding_first_lease(
    pool: AsyncConnectionPool,
    request_ids: range,
    *,
    acquired: asyncio.Queue[None],
    release: asyncio.Event,
    backend_ids: set[int],
) -> None:
    """Commit each request in its own short transaction; after the first one
    keep the client lease at the barrier until `release`."""
    for offset, request_id in enumerate(request_ids):
        async with pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO host_pool_capacity_results VALUES (%s, %s)",
                    (request_id, request_id * 7),
                )
                row = await (await conn.execute("SELECT pg_backend_pid()")).fetchone()
                assert row is not None
                backend_ids.add(row[0])
            if offset == 0:
                acquired.put_nowait(None)
                await release.wait()


async def _observe_barrier_then_release(
    controls: list[AsyncConnectionPool],
    admin: psycopg.AsyncConnection[DictRow],
    pooled: str,
    *,
    worker_count: int,
    acquired: asyncio.Queue[None],
    release: asyncio.Event,
    record_property: Callable[[str, object], None],
) -> None:
    """Wait for every worker's first lease, check PgBouncer's counts and the
    control pools, then let the workers run on."""
    try:
        for _ in range(worker_count):
            await acquired.get()
        await asyncio.gather(*(_control_query(pool) for pool in controls))
        counts = await _pool_counts(admin, str(conninfo_to_dict(pooled)["dbname"]))
        assert worker_count + _RUNNERS <= counts["clients"] < 100
        assert 0 < counts["servers"] <= _BACKENDS
        record_property("pgbouncer_clients_at_barrier", counts["clients"])
        record_property("pgbouncer_servers_at_barrier", counts["servers"])
    finally:
        release.set()


async def _settle_requests_across_pools(
    workloads: list[AsyncConnectionPool],
    controls: list[AsyncConnectionPool],
    admin: psycopg.AsyncConnection[DictRow],
    pooled: str,
    record_property: Callable[[str, object], None],
) -> set[int]:
    """Run the worker fleet through the barrier; returns the backend pids seen."""
    worker_count = _RUNNERS * _BORROWERS_PER_RUNNER
    acquired: asyncio.Queue[None] = asyncio.Queue()
    release = asyncio.Event()
    backend_ids: set[int] = set()
    async with asyncio.timeout(60), asyncio.TaskGroup() as tasks:
        for worker_id in range(worker_count):
            tasks.create_task(
                _insert_requests_holding_first_lease(
                    workloads[worker_id % _RUNNERS],
                    range(_RUNNERS + worker_id, _REQUESTS, worker_count),
                    acquired=acquired,
                    release=release,
                    backend_ids=backend_ids,
                )
            )
        await _observe_barrier_then_release(
            controls,
            admin,
            pooled,
            worker_count=worker_count,
            acquired=acquired,
            release=release,
            record_property=record_property,
        )
        # These control queries now compete with ongoing short workload
        # transactions. Completion is required; no latency SLO is claimed.
        for pool in controls:
            tasks.create_task(_control_query(pool))
    return backend_ids


async def test_six_host_pools_settle_one_thousand_short_requests_through_pgbouncer(
    record_property: Callable[[str, object], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise six pool pairs without claiming a thousand full agent turns.

    Seventy-two workers keep the fixture below its 100-client ceiling, although
    each pool uses the production defaults. The first request holds every client
    lease at a barrier so PgBouncer's actual client/server counts are observable.
    Six requests are cancelled in PostgreSQL; all other IDs must commit once.
    """
    started = time.monotonic()
    with postgres() as direct, _pgbouncer_in_front(direct, pool_size=_BACKENDS) as pooled:
        # The host pools dial the cluster's access URL, which is this pooler.
        monkeypatch.setattr(settings.data_plane, "db_url", pooled)
        async with AsyncExitStack() as stack:
            observer = await stack.enter_async_context(
                await psycopg.AsyncConnection.connect(direct, autocommit=True)
            )
            await observer.execute(
                "CREATE TABLE host_pool_capacity_results (request_id integer PRIMARY KEY, value integer)"
            )
            admin = await stack.enter_async_context(
                await psycopg.AsyncConnection[DictRow].connect(
                    _admin_console_url(pooled), autocommit=True, row_factory=dict_row
                )
            )
            workloads = [
                await stack.enter_async_context(build_shared_pool(Database.from_settings()))
                for _ in range(_RUNNERS)
            ]
            controls = [
                await stack.enter_async_context(build_control_pool(Database.from_settings()))
                for _ in range(_RUNNERS)
            ]
            backend_ids = await _settle_requests_across_pools(
                workloads, controls, admin, pooled, record_property
            )

            assert 0 < len(backend_ids) <= _BACKENDS
            for request_id, pool in enumerate(workloads):
                await _cancel_blocked_transaction(pool, observer, request_id)

            rows = await (
                await observer.execute("SELECT request_id, value FROM host_pool_capacity_results")
            ).fetchall()
            assert dict(rows) == {
                request_id: request_id * 7 for request_id in range(_RUNNERS, _REQUESTS)
            }
            for pool in [*workloads, *controls]:
                assert pool.get_stats()["requests_waiting"] == 0
                assert pool.get_stats()["pool_size"] <= pool.max_size
            record_property("logical_requests", _REQUESTS)
            record_property("committed_requests", len(rows))
            record_property("cancelled_transactions", _RUNNERS)
            record_property("observed_backend_pids", len(backend_ids))
            record_property("elapsed_seconds", time.monotonic() - started)
