"""Only the original target-bound lifecycle receipt proves lost-commit outcomes."""

from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent.ownership.hosted import apply_hosted_lifecycle
from agent.ownership.hosted_completion import (
    completed_hosted_lifecycle_kind,
    pending_hosted_lifecycle_id,
)
from agent.ownership.lifecycle_intent import accept_lifecycle_intent, observe_hosted_admission
from agent.ownership.tests.test_lifecycle_intent import _command
from agent.tests.claim.test_inbound_ownership import _admit, _agent
from base.db.transaction import async_write_transaction
from base.events.live.bus import EventBus
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.native_process.turn_identity import bind_turn_identity


@pytest.mark.parametrize("kind", ["restart", "terminate"])
async def test_completion_requires_original_applied_and_observed_receipt(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    event_bus: EventBus,
    kind: str,
) -> None:
    agent = _agent(db_conn)
    original = await _admit(aops_pool, agent)
    command = _command(db_conn, agent, kind)
    with bind_turn_identity(agent, incarnation=original):
        async with async_write_transaction(aops_pool) as conn:
            await accept_lifecycle_intent(conn, agent)
        assert await pending_hosted_lifecycle_id(aops_pool, original) == command
        # A graph's exit flag or mutable status does not certify this receipt.
        assert await completed_hosted_lifecycle_kind(aops_pool, original, command) is None
        assert (
            await apply_hosted_lifecycle(
                aops_pool, original, bus=event_bus, expected_command_id=command + 1
            )
            is None
        )
        assert await pending_hosted_lifecycle_id(aops_pool, original) == command
        assert (
            await apply_hosted_lifecycle(
                aops_pool, original, bus=event_bus, expected_command_id=command
            )
            == kind
        )
    assert await completed_hosted_lifecycle_kind(aops_pool, original, command) == kind
    if kind == "restart":
        replacement = await _admit(aops_pool, agent)
        async with async_write_transaction(aops_pool) as conn:
            await observe_hosted_admission(conn, replacement)
        # A successor may have observed the restart before the old host reads
        # its lost acknowledgement. The retained original receipt still proves it.
        assert await completed_hosted_lifecycle_kind(aops_pool, original, command) == kind
    assert await completed_hosted_lifecycle_kind(aops_pool, original, command + 1) is None
    for wrong in (
        RuntimeIncarnation(agent, uuid4(), original.owner),
        RuntimeIncarnation(agent, original.generation, uuid4()),
        RuntimeIncarnation(agent + 1, original.generation, original.owner),
    ):
        assert await completed_hosted_lifecycle_kind(aops_pool, wrong, command) is None
    if kind == "terminate":
        db_conn.execute("UPDATE inbound_messages SET observed_at=NULL WHERE id=%s", (command,))
        db_conn.commit()
        assert await completed_hosted_lifecycle_kind(aops_pool, original, command) is None


@pytest.mark.parametrize("kind", ["restart", "terminate"])
async def test_receipt_does_not_infer_completion_from_replacement_status(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    kind: str,
) -> None:
    agent = _agent(db_conn)
    original = await _admit(aops_pool, agent)
    command = _command(db_conn, agent, kind)
    with bind_turn_identity(agent, incarnation=original):
        async with async_write_transaction(aops_pool) as conn:
            await accept_lifecycle_intent(conn, agent)
    db_conn.execute(
        "UPDATE agents_meta SET status='terminated',termination_source='user',"
        "runtime_generation=%s,runtime_owner=%s WHERE id=%s",
        (uuid4(), uuid4(), agent),
    )
    db_conn.commit()
    assert await completed_hosted_lifecycle_kind(aops_pool, original, command) is None
