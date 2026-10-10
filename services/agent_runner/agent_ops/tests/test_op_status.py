"""Stored op replay validates terminal results while NULL remains an unfinished owner."""

import hashlib
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db import pool as db_pool
from base.lm.catalog import ModelCatalog
from base.native_process.loaded_commit import LoadedCommit
from ops.rpc_schemas import OpStatus
from services.agent_runner.agent_ops import daemon


@pytest.fixture
def pool() -> Iterator[ConnectionPool]:
    resource = db_pool(max_size=2)
    try:
        yield resource
    finally:
        resource.close()


def _record(conn: psycopg.Connection, status: str | None) -> None:
    conn.execute(
        "INSERT INTO api_idempotency(key,method,path,op_status,response_body,completed_at,request_hash) "
        "VALUES ('status-test','ops','status_probe',%s,'{}',now(),%s)",
        (status, hashlib.sha256(b'["status_probe",{}]').hexdigest()),
    )
    conn.commit()


@pytest.mark.parametrize("status", list(OpStatus))
async def test_replay_returns_canonical_status_without_reexecution(
    op_executor: ThreadPoolExecutor,
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    status: OpStatus,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    ops_database: Callable[[], Database],
    ops_image: LoadedCommit,
) -> None:
    _record(db_conn, status.value)
    actual, result = await daemon._dispatch_idempotent_pass(
        "status_probe",
        {},
        "status-test",
        pool,
        active_ops={},
        workers=set(),
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
        database=ops_database,
        image=ops_image,
    )
    assert actual is status
    assert result == {}


@pytest.mark.parametrize("status", ["pending", "", "enqueued"])
async def test_replay_rejects_unknown_stored_status_without_reexecution(
    op_executor: ThreadPoolExecutor,
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    status: str,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    ops_database: Callable[[], Database],
    ops_image: LoadedCommit,
) -> None:
    _record(db_conn, status)
    with pytest.raises(ValueError, match="OpStatus"):
        await daemon._dispatch_idempotent_pass(
            "status_probe",
            {},
            "status-test",
            pool,
            active_ops={},
            workers=set(),
            executor=op_executor,
            catalog=model_catalog,
            authority=config_authority,
            database=ops_database,
            image=ops_image,
        )
    assert db_conn.execute(
        "SELECT op_status FROM api_idempotency WHERE key='status-test'"
    ).fetchone() == (status,)


async def test_null_still_waits_for_owner_instead_of_becoming_a_result(
    op_executor: ThreadPoolExecutor,
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    ops_database: Callable[[], Database],
    ops_image: LoadedCommit,
) -> None:
    _record(db_conn, None)
    monkeypatch.setattr(daemon, "_DEDUP_WAIT_ATTEMPTS", 1)

    async def sleep(_duration: float) -> None:
        return None

    monkeypatch.setattr(daemon, "_sleep", sleep)
    status, result = await daemon._dispatch_idempotent_pass(
        "status_probe",
        {},
        "status-test",
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
    assert "never completed" in str(result["error"])
