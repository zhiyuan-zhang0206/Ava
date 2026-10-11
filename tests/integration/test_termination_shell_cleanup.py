"""Termination commits before a bounded selection of best-effort PTY cleanup."""

import asyncio
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from agent.db import claim_inbound_batch
from agent.ownership.hosted import apply_hosted_lifecycle
from agent.tests.claim.test_inbound_ownership import _admit, agent_row
from base.agents.incarnation.hosted_force import original_host_force, recover_orphaned_hosted_forces
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.native_process.runtime_incarnation import RuntimeIncarnation
from ops.lifecycle.termination import _force_terminate_transaction
from services.agent_runner.agent_host.tests.lifecycle.test_hosted_lifecycle import (
    _terminate_command,
)


async def _prepare(
    phase: str, conn: psycopg.Connection, pool: AsyncConnectionPool, *, database_gate: ProcessDbGate
) -> tuple[RuntimeIncarnation, int]:
    agent_id = agent_row(conn)
    owner = await _admit(pool, agent_id, database_gate=database_gate)
    conn.execute("UPDATE agents_meta SET session_index=4 WHERE id=%s", (agent_id,))
    conn.commit()
    if phase == "graceful":
        command = _terminate_command(conn, agent_id, kill=True)
        await claim_inbound_batch(pool, agent_id, incarnation=owner, work=None)
    else:
        with ConnectionPool[psycopg.Connection](conn.info.dsn) as sync_pool:
            receipt = await asyncio.to_thread(
                _force_terminate_transaction,
                agent_id,
                sync_pool,
                source="user",
                kill_all_shell_sessions=True,
            )
        command = receipt[3]
        assert receipt[4] == 4  # K1; old work allocates more IDs before settlement.
    conn.execute("UPDATE agents_meta SET session_index=7 WHERE id=%s", (agent_id,))
    conn.commit()
    return owner, command


async def _settle(
    phase: str,
    pool: AsyncConnectionPool,
    owner: RuntimeIncarnation,
    command: int,
    bus: EventBus,
    killer: Callable[[int, int], None],
) -> object:
    if phase == "graceful":
        return await apply_hosted_lifecycle(
            pool, owner, bus=bus, resources=None, kill_shell_sessions=killer
        )
    if phase == "force":
        return await original_host_force(
            pool,
            owner.agent_id,
            owner.owner,
            "claim-test",
            command_id=command,
            quiescent=True,
            kill_shell_sessions=killer,
        )
    return await recover_orphaned_hosted_forces(pool, "claim-test", kill_shell_sessions=killer)


def _assert_committed_and_allocate(dsn: str, agent_id: int, command: int) -> None:
    """An independent connection can see the receipt and allocate past K2."""
    with psycopg.connect(dsn) as conn:
        conn.execute("SET LOCAL lock_timeout='500ms'")
        assert conn.execute(
            "SELECT status,lifecycle_command_id FROM agents_meta WHERE id=%s", (agent_id,)
        ).fetchone() == ("terminated", None)
        assert conn.execute(
            "SELECT status,observed_at IS NOT NULL FROM inbound_messages WHERE id=%s", (command,)
        ).fetchone() == ("done", True)
        assert conn.execute(
            "UPDATE agents_meta SET session_index=session_index+1 WHERE id=%s "
            "RETURNING session_index-1",
            (agent_id,),
        ).fetchone() == (7,)


@pytest.mark.parametrize("phase", ["graceful", "force", "boot"])
async def test_blocked_shell_cleanup_releases_row_lock_with_fixed_cutoff(
    phase: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    event_bus: EventBus,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    database_gate: ProcessDbGate,
) -> None:
    monkeypatch.setattr(
        "base.agents.incarnation.exec_request_evidence.exec_run_dir", lambda: tmp_path
    )
    owner, command = await _prepare(phase, db_conn, aops_pool, database_gate=database_gate)
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    cutoffs: list[int] = []

    def blocked_kill(agent_id: int, cutoff: int) -> None:
        assert agent_id == owner.agent_id
        cutoffs.append(cutoff)
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5), "test must release its cleanup worker"

    task = asyncio.create_task(_settle(phase, aops_pool, owner, command, event_bus, blocked_kill))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        _assert_committed_and_allocate(db_conn.info.dsn, owner.agent_id, command)
        assert cutoffs == [7] and not task.done()
    finally:
        release.set()
        await asyncio.wait_for(task, 3)
    assert cutoffs == [7]  # No chase of the just-allocated ID 7.


@pytest.mark.parametrize("phase", ["graceful", "force", "boot"])
async def test_unknown_cleanup_error_keeps_original_exception_and_committed_receipt(
    phase: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    event_bus: EventBus,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    database_gate: ProcessDbGate,
) -> None:
    monkeypatch.setattr(
        "base.agents.incarnation.exec_request_evidence.exec_run_dir", lambda: tmp_path
    )
    owner, command = await _prepare(phase, db_conn, aops_pool, database_gate=database_gate)
    original = RuntimeError("unknown cleanup defect")

    def failed_kill(agent_id: int, cutoff: int) -> None:
        assert (agent_id, cutoff) == (owner.agent_id, 7)
        raise original

    with pytest.raises(RuntimeError) as caught:
        await _settle(phase, aops_pool, owner, command, event_bus, failed_kill)
    assert caught.value is original
    _assert_committed_and_allocate(db_conn.info.dsn, owner.agent_id, command)


async def test_publication_failure_still_attempts_cleanup_and_keeps_primary_error(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    event_bus: EventBus,
    monkeypatch: pytest.MonkeyPatch,
    loguru_records: list[dict[str, Any]],
    *,
    database_gate: ProcessDbGate,
) -> None:
    owner, command = await _prepare("graceful", db_conn, aops_pool, database_gate=database_gate)
    original = RuntimeError("unknown event bus defect")
    secondary = RuntimeError("unknown cleanup defect")
    calls: list[tuple[int, int]] = []

    async def failed_publish(bus: EventBus, agent_id: int) -> None:
        assert bus is event_bus and agent_id == owner.agent_id
        raise original

    def failed_kill(agent_id: int, cutoff: int) -> None:
        calls.append((agent_id, cutoff))
        raise secondary

    monkeypatch.setattr("agent.ownership.hosted_cleanup.publish_agent_updated", failed_publish)
    with pytest.raises(RuntimeError) as caught:
        await _settle("graceful", aops_pool, owner, command, event_bus, failed_kill)
    assert caught.value is original
    assert calls == [(owner.agent_id, 7)]
    assert any("Post-commit shell cleanup also failed" in note for note in original.__notes__)
    assert any(
        record["exception"].value is secondary for record in loguru_records if record["exception"]
    )
    _assert_committed_and_allocate(db_conn.info.dsn, owner.agent_id, command)
