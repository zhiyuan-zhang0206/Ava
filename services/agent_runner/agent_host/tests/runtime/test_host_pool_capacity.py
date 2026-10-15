import asyncio
import time
from collections.abc import Callable
from contextlib import AsyncExitStack

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool

from base.clock import SYSTEM_TIMERS
from base.cluster.dataplane.pg_throwaway_base import ThrowawayConfig
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config import settings
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.deploy.tests.containers import postgres
from base.deploy.tests.pgbouncer import (
    admin_console_url,
    pgbouncer_available,
    pgbouncer_in_front,
)
from services.agent_runner.agent_host.pools import build_control_pool, build_shared_pool

pytestmark = pytest.mark.skipif(
    not pgbouncer_available(), reason="pgbouncer not installed (brew/apt install pgbouncer)"
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
    async with asyncio.timeout(15):
        for _ in range(borrower_count):
            await acquired.get()
        queued = tasks.create_task(_control_query(workload))
        while workload.get_stats()["requests_waiting"] == 0:
            await asyncio.sleep(0)
        assert workload.get_stats()["requests_waiting"] == 1
        assert not queued.done()
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
    postgres_access: None,
    record_property: Callable[[str, object], None],
    monkeypatch: pytest.MonkeyPatch,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
    throwaway_config: ThrowawayConfig,
) -> None:
    with (
        postgres(config=throwaway_config, machine_name=machine_identity.name) as direct,
        pgbouncer_in_front(direct, pool_size=_BACKENDS) as pooled,
    ):
        monkeypatch.setattr(settings.data_plane, "db_url", pooled)
        async with (
            build_shared_pool(
                Database.from_settings(gate=database_gate), timers=SYSTEM_TIMERS
            ) as workload,
            build_control_pool(
                Database.from_settings(gate=database_gate), timers=SYSTEM_TIMERS
            ) as control,
            await psycopg.AsyncConnection[DictRow].connect(
                admin_console_url(pooled), autocommit=True, row_factory=dict_row
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
        for pool in controls:
            tasks.create_task(_control_query(pool))
    return backend_ids


async def test_six_host_pools_settle_one_thousand_short_requests_through_pgbouncer(
    postgres_access: None,
    record_property: Callable[[str, object], None],
    monkeypatch: pytest.MonkeyPatch,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
    throwaway_config: ThrowawayConfig,
) -> None:
    started = time.monotonic()
    with (
        postgres(config=throwaway_config, machine_name=machine_identity.name) as direct,
        pgbouncer_in_front(direct, pool_size=_BACKENDS) as pooled,
    ):
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
                    admin_console_url(pooled), autocommit=True, row_factory=dict_row
                )
            )
            workloads = [
                await stack.enter_async_context(
                    build_shared_pool(
                        Database.from_settings(gate=database_gate), timers=SYSTEM_TIMERS
                    )
                )
                for _ in range(_RUNNERS)
            ]
            controls = [
                await stack.enter_async_context(
                    build_control_pool(
                        Database.from_settings(gate=database_gate), timers=SYSTEM_TIMERS
                    )
                )
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
