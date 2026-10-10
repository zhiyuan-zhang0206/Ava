"""Immutable ops identity and crash-window protection on real database rows."""

import asyncio
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import LiteralString, cast

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db import pool as db_pool
from base.db.code_version_gate import ProcessDbGate
from base.lm.catalog import ModelCatalog
from base.native_process.loaded_commit import LoadedCommit
from ops.rpc_schemas import OpStatus
from services.agent_runner.agent_ops import daemon
from services.agent_runner.agent_ops.maintenance import WorkerFutures


@pytest.fixture
def pool(*, database_gate: ProcessDbGate) -> Iterator[ConnectionPool]:
    resource = db_pool(max_size=2, gate=database_gate)
    try:
        yield resource
    finally:
        resource.close()


@pytest.fixture
def dispatches(monkeypatch: pytest.MonkeyPatch, pool: ConnectionPool) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []
    dispatch_pool = pool

    async def dispatch(
        kind: str,
        payload: dict[str, object],
        *,
        active_ops: daemon.ActiveOps,
        workers: WorkerFutures,
        pool: ConnectionPool,
        executor: ThreadPoolExecutor,
        catalog: ModelCatalog,
        authority: ConfigAuthority,
        database: Callable[[], Database],
        image: LoadedCommit,
    ) -> tuple[OpStatus, dict[str, object]]:
        assert pool is dispatch_pool
        calls.append({"kind": kind, "payload": payload})
        return OpStatus.COMPLETED, {"accepted": True}

    monkeypatch.setattr(daemon, "_dispatch", dispatch)
    monkeypatch.setattr(daemon, "_DEDUP_WAIT_ATTEMPTS", 1)

    async def sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(daemon, "_sleep", sleep)
    return calls


@pytest.mark.parametrize(
    ("kind", "payload"),
    [("status_probe", {"target": 2}), ("lifecycle", {"target": 1})],
)
async def test_changed_kind_or_payload_fails_without_dispatch(
    op_executor: ThreadPoolExecutor,
    pool: ConnectionPool,
    dispatches: list[dict[str, object]],
    kind: str,
    payload: dict[str, object],
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    ops_database: Callable[[], Database],
    ops_image: LoadedCommit,
) -> None:
    await daemon._dispatch_idempotent_pass(
        "status_probe",
        {"target": 1},
        "intent",
        pool,
        active_ops={},
        workers=set(),
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
        database=ops_database,
        image=ops_image,
    )
    status, result = await daemon._dispatch_idempotent_pass(
        kind,
        payload,
        "intent",
        pool,
        active_ops={},
        workers=set(),
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
        database=ops_database,
        image=ops_image,
    )
    assert status is OpStatus.FAILED
    assert "identity conflict" in str(result["error"])
    assert len(dispatches) == 1


async def test_key_order_does_not_change_request_identity(
    op_executor: ThreadPoolExecutor,
    pool: ConnectionPool,
    dispatches: list[dict[str, object]],
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    ops_database: Callable[[], Database],
    ops_image: LoadedCommit,
) -> None:
    first = await daemon._dispatch_idempotent_pass(
        "status_probe",
        {"a": 1, "b": 2},
        "intent",
        pool,
        active_ops={},
        workers=set(),
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
        database=ops_database,
        image=ops_image,
    )
    second = await daemon._dispatch_idempotent_pass(
        "status_probe",
        {"b": 2, "a": 1},
        "intent",
        pool,
        active_ops={},
        workers=set(),
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
        database=ops_database,
        image=ops_image,
    )
    assert second == first
    assert len(dispatches) == 1


async def test_effect_then_exception_keeps_uncertain_claim(
    op_executor: ThreadPoolExecutor,
    pool: ConnectionPool,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    dispatches: list[dict[str, object]],
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    ops_database: Callable[[], Database],
    ops_image: LoadedCommit,
) -> None:
    async def crash(
        kind: str,
        payload: dict[str, object],
        *,
        active_ops: daemon.ActiveOps,
        workers: WorkerFutures,
        pool: ConnectionPool,
        executor: ThreadPoolExecutor,
        catalog: ModelCatalog,
        authority: ConfigAuthority,
        database: Callable[[], Database],
        image: LoadedCommit,
    ) -> tuple[OpStatus, dict[str, object]]:
        with pool.connection() as conn:
            conn.execute("INSERT INTO agents (label) VALUES ('effect-before-crash')")
            conn.commit()
        raise RuntimeError("died after effect")

    monkeypatch.setattr(daemon, "_dispatch", crash)
    with pytest.raises(RuntimeError, match="after effect"):
        await daemon._dispatch_idempotent_pass(
            "lifecycle",
            {},
            "intent",
            pool,
            active_ops={},
            workers=set(),
            executor=op_executor,
            catalog=model_catalog,
            authority=config_authority,
            database=ops_database,
            image=ops_image,
        )
    status, result = await daemon._dispatch_idempotent_pass(
        "lifecycle",
        {},
        "intent",
        pool,
        active_ops={},
        workers=set(),
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
        database=ops_database,
        image=ops_image,
    )
    assert status is OpStatus.FAILED
    assert "outcome uncertain" in str(result["error"])
    assert db_conn.execute(
        "SELECT count(*) FROM agents WHERE label='effect-before-crash'"
    ).fetchone() == (1,)
    assert db_conn.execute(
        "SELECT op_status, completed_at FROM api_idempotency WHERE key='intent'"
    ).fetchone() == (None, None)


async def test_owner_cancellation_never_frees_its_key(
    op_executor: ThreadPoolExecutor,
    pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    dispatches: list[dict[str, object]],
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    ops_database: Callable[[], Database],
    ops_image: LoadedCommit,
) -> None:
    started = asyncio.Event()
    calls = 0

    async def interrupted(
        kind: str,
        payload: dict[str, object],
        *,
        active_ops: daemon.ActiveOps,
        workers: WorkerFutures,
        pool: ConnectionPool,
        executor: ThreadPoolExecutor,
        catalog: ModelCatalog,
        authority: ConfigAuthority,
        database: Callable[[], Database],
        image: LoadedCommit,
    ) -> tuple[OpStatus, dict[str, object]]:
        nonlocal calls
        calls += 1
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(daemon, "_dispatch", interrupted)
    owner = asyncio.create_task(
        daemon._dispatch_idempotent_pass(
            "lifecycle",
            {},
            "intent",
            pool,
            active_ops={},
            workers=set(),
            executor=op_executor,
            catalog=model_catalog,
            authority=config_authority,
            database=ops_database,
            image=ops_image,
        )
    )
    await started.wait()
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner
    status, result = await daemon._dispatch_idempotent_pass(
        "lifecycle",
        {},
        "intent",
        pool,
        active_ops={},
        workers=set(),
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
        database=ops_database,
        image=ops_image,
    )
    assert status is OpStatus.FAILED
    assert "outcome uncertain" in str(result["error"])
    assert calls == 1


async def test_old_ops_receipt_does_not_expire_into_fresh_execution(
    op_executor: ThreadPoolExecutor,
    pool: ConnectionPool,
    db_conn: psycopg.Connection,
    dispatches: list[dict[str, object]],
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    ops_database: Callable[[], Database],
    ops_image: LoadedCommit,
) -> None:
    first = await daemon._dispatch_idempotent_pass(
        "status_probe",
        {},
        "intent",
        pool,
        active_ops={},
        workers=set(),
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
        database=ops_database,
        image=ops_image,
    )
    db_conn.execute("UPDATE api_idempotency SET completed_at=now()-interval '8 days'")
    db_conn.commit()
    second = await daemon._dispatch_idempotent_pass(
        "status_probe",
        {},
        "intent",
        pool,
        active_ops={},
        workers=set(),
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
        database=ops_database,
        image=ops_image,
    )
    assert second == first
    assert len(dispatches) == 1


async def test_unknown_legacy_identity_fails_closed(
    op_executor: ThreadPoolExecutor,
    pool: ConnectionPool,
    db_conn: psycopg.Connection,
    dispatches: list[dict[str, object]],
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    ops_database: Callable[[], Database],
    ops_image: LoadedCommit,
) -> None:
    db_conn.execute(
        "INSERT INTO api_idempotency(key,method,path,op_status,response_body) "
        "VALUES ('intent','ops','status_probe','completed','{}')"
    )
    db_conn.commit()
    status, result = await daemon._dispatch_idempotent_pass(
        "status_probe",
        {},
        "intent",
        pool,
        active_ops={},
        workers=set(),
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
        database=ops_database,
        image=ops_image,
    )
    assert status is OpStatus.FAILED
    assert "legacy identity unavailable" in str(result["error"])
    assert dispatches == []


async def test_result_write_failure_keeps_claim_without_reexecuting(
    op_executor: ThreadPoolExecutor,
    pool: ConnectionPool,
    db_conn: psycopg.Connection,
    dispatches: list[dict[str, object]],
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    ops_database: Callable[[], Database],
    ops_image: LoadedCommit,
) -> None:
    db_conn.execute(
        "ALTER TABLE api_idempotency ADD CONSTRAINT reject_test_result "
        "CHECK (key <> 'intent' OR op_status IS NULL)"
    )
    db_conn.commit()
    try:
        with pytest.raises(psycopg.errors.CheckViolation):
            await daemon._dispatch_idempotent_pass(
                "lifecycle",
                {},
                "intent",
                pool,
                active_ops={},
                workers=set(),
                executor=op_executor,
                catalog=model_catalog,
                authority=config_authority,
                database=ops_database,
                image=ops_image,
            )
    finally:
        db_conn.execute("ALTER TABLE api_idempotency DROP CONSTRAINT reject_test_result")
        db_conn.commit()
    status, result = await daemon._dispatch_idempotent_pass(
        "lifecycle",
        {},
        "intent",
        pool,
        active_ops={},
        workers=set(),
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
        database=ops_database,
        image=ops_image,
    )
    assert status is OpStatus.FAILED
    assert "outcome uncertain" in str(result["error"])
    assert len(dispatches) == 1


def test_request_identity_migration_upgrades_and_replays(db_conn: psycopg.Connection) -> None:
    migration = Path(__file__).resolve().parents[4] / (
        "migrations/2026/10/07/10/20261007T103915_ops-request-identity.sql"
    )
    with db_conn.transaction():
        db_conn.execute("ALTER TABLE api_idempotency DROP COLUMN request_hash")
        db_conn.execute(cast(LiteralString, migration.read_text()))
        db_conn.execute(cast(LiteralString, migration.read_text()))
        row = db_conn.execute(
            "SELECT data_type, is_nullable FROM information_schema.columns "
            "WHERE table_name='api_idempotency' AND column_name='request_hash'"
        ).fetchone()
        assert row == ("text", "YES")
