"""Native intent admission is immutable and does not enter generic inbound."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool, ConnectionPool
from pydantic import ValidationError

from agent.db import claim_inbound_batch
from agent.tests.test_inbound_ownership import _insert
from base.agents.incarnation.native_work_models import NativeCancelPendingError, NativeWorkTarget
from base.agents.messages.native_cancel import (
    NativeCancelConflictError,
    accept_native_cancel,
    observe_native_work,
)
from base.native_process.turn_identity import bind_native_work, bind_turn_identity
from services.agent_runner.agent_host.tests.native_cancel.helpers import managed_work


async def test_receipt_precedes_mutable_work_and_owner_state(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    pool: ConnectionPool
    incarnation, target = await managed_work(db_conn, aops_pool)
    with ConnectionPool[psycopg.Connection](db_conn.info.dsn) as pool:
        assert await asyncio.to_thread(observe_native_work, pool, target.agent_id) == target
        accepted = await asyncio.to_thread(
            accept_native_cancel, pool, "cancel-first", target.agent_id, target
        )
        db_conn.execute(
            "UPDATE agents_meta SET native_work_id=NULL,runtime_owner=%s WHERE id=%s",
            (uuid4(), target.agent_id),
        )
        db_conn.execute("DELETE FROM native_graph_work WHERE id=%s", (target.work_id,))
        db_conn.commit()
        assert (
            await asyncio.to_thread(
                accept_native_cancel, pool, "cancel-first", target.agent_id, target
            )
            == accepted
        )
        with pytest.raises(NativeCancelConflictError, match="another target"):
            await asyncio.to_thread(
                accept_native_cancel,
                pool,
                "cancel-first",
                target.agent_id,
                target.model_copy(update={"work_id": uuid4()}),
            )
        assert incarnation.agent_id == target.agent_id
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (target.agent_id,)
    ).fetchone() == (0,)


async def test_two_connections_first_key_owns_work(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    pool: ConnectionPool
    _incarnation, target = await managed_work(db_conn, aops_pool)
    with (
        ConnectionPool[psycopg.Connection](db_conn.info.dsn, min_size=2, max_size=2) as pool,
        ThreadPoolExecutor(2) as executor,
    ):
        results = await asyncio.gather(
            *[
                asyncio.get_running_loop().run_in_executor(
                    executor, accept_native_cancel, pool, key, target.agent_id, target
                )
                for key in ("cancel-one", "cancel-two")
            ],
            return_exceptions=True,
        )
    assert sum(isinstance(value, NativeCancelConflictError) for value in results) == 1
    assert db_conn.execute(
        "SELECT count(*) FROM native_cancel_commands WHERE work_id=%s", (target.work_id,)
    ).fetchone() == (1,)


async def test_preparing_refused_and_cancel_claim_guard_leaves_chat_pending(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    pool: ConnectionPool
    incarnation, target = await managed_work(db_conn, aops_pool, active=False)
    with ConnectionPool[psycopg.Connection](db_conn.info.dsn) as pool:
        assert await asyncio.to_thread(observe_native_work, pool, target.agent_id) is None
        with pytest.raises(NativeCancelConflictError, match="not eligible"):
            await asyncio.to_thread(
                accept_native_cancel, pool, "preparing", target.agent_id, target
            )
        db_conn.execute(
            "UPDATE native_graph_work SET phase='active' WHERE id=%s", (target.work_id,)
        )
        db_conn.commit()
        chat = _insert(db_conn, target.agent_id)
        await asyncio.to_thread(accept_native_cancel, pool, "ready", target.agent_id, target)
        with (
            bind_turn_identity(target.agent_id, incarnation=incarnation),
            bind_native_work(target.work_id),
            pytest.raises(NativeCancelPendingError),
        ):
            await claim_inbound_batch(aops_pool, target.agent_id)
    assert db_conn.execute(
        "SELECT status,claimed_at FROM inbound_messages WHERE id=%s", (chat,)
    ).fetchone() == ("pending", None)


@pytest.mark.parametrize(
    "field,value",
    [("agent_id", True), ("agent_id", 1.0), ("protocol", True), ("protocol", 1.0), ("protocol", 2)],
)
def test_target_boundary_refuses_fabricated_qualification(field: str, value: object) -> None:
    raw: dict[str, object] = {
        "agent_id": 1,
        "work_id": str(uuid4()),
        "machine": "test",
        "generation": str(uuid4()),
        "owner": str(uuid4()),
        "protocol": 1,
    }
    raw[field] = value
    with pytest.raises(ValidationError):
        NativeWorkTarget.model_validate(raw)


def test_protocol_cannot_be_inferred_from_a_missing_observation() -> None:
    with pytest.raises(ValidationError):
        NativeWorkTarget.model_validate(
            {
                "agent_id": 1,
                "work_id": str(uuid4()),
                "machine": "test",
                "generation": str(uuid4()),
                "owner": str(uuid4()),
            }
        )


async def test_same_key_two_real_connections_share_one_original_receipt(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    pool: ConnectionPool
    _incarnation, target = await managed_work(db_conn, aops_pool)
    with (
        ConnectionPool[psycopg.Connection](db_conn.info.dsn, min_size=2, max_size=2) as pool,
        ThreadPoolExecutor(2) as executor,
    ):
        results = await asyncio.gather(
            *[
                asyncio.get_running_loop().run_in_executor(
                    executor, accept_native_cancel, pool, "same-native-key", target.agent_id, target
                )
                for _ in range(2)
            ]
        )
    assert results[0] == results[1]
    assert db_conn.execute(
        "SELECT count(*) FROM native_cancel_commands WHERE work_id=%s", (target.work_id,)
    ).fetchone() == (1,)
