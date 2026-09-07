"""Keep an interrupted host turn alive until its database is usable again."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import psycopg
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph.state import CompiledStateGraph
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from agent.impersonation import flush_checkpoint
from agent.inbound_ownership import RuntimeOwnershipLostError
from agent.startup import (
    _reconcile_claimed_inbounds_at_startup,
    _repair_dangling_tool_use_at_startup,
)
from shared.db_transaction import async_write_transaction
from shared.deploy_timing import AGENT_LEASE_TTL_S
from shared.hosted_db_wait import database_wait
from shared.log import logger
from shared.runtime_incarnation import RuntimeIncarnation, current_incarnation

_PROBE_TIMEOUT_SECONDS = 5.0
_RECOVERY_TIMEOUT_SECONDS = 5.0
_INITIAL_BACKOFF_SECONDS = 1.0
_MAX_BACKOFF_SECONDS = 30.0
_DATABASE_PHASE_TIMEOUT_SECONDS = 30.0


@asynccontextmanager
async def database_phase() -> AsyncGenerator[None]:
    """Bound host DB-only transitions, outside graph/LLM/owned code execution."""
    try:
        async with asyncio.timeout(_DATABASE_PHASE_TIMEOUT_SECONDS):
            yield
    except TimeoutError as exc:
        raise PoolTimeout("host database phase timed out") from exc


async def _refresh_owner(pool: AsyncConnectionPool, incarnation: RuntimeIncarnation) -> None:
    """A live original task may renew an expired lease, never a released owner.

    The conditional UPDATE serializes with admission and force acceptance. An
    outage may outlast the lease; the exact generation and owner must still be
    present, with neither a NULL lease nor a frozen/applied lifecycle decision.
    Pending ordinary restart/terminate remains claimable by this continuation.
    """
    try:
        async with asyncio.timeout(_PROBE_TIMEOUT_SECONDS):
            async with async_write_transaction(pool, timeout=_PROBE_TIMEOUT_SECONDS) as conn:
                cursor = await conn.execute(
                    "UPDATE agents_meta SET lease_expires_at=clock_timestamp() "
                    "+ make_interval(secs => %s) "
                    "WHERE id=%s AND runtime_generation=%s AND runtime_owner=%s "
                    "AND runtime_kind='hosted' AND status IN ('running','idling') "
                    "AND lease_expires_at IS NOT NULL "
                    "AND (incarnation_resources IS NULL OR ("
                    "incarnation_resources->>'state'='admitted' "
                    "AND incarnation_resources->>'generation'=%s "
                    "AND incarnation_resources->>'owner'=%s "
                    "AND incarnation_resources->>'frozen_by' IS NULL)) "
                    "AND NOT EXISTS (SELECT 1 FROM inbound_messages i "
                    "WHERE i.id=agents_meta.lifecycle_command_id AND i.applied_at IS NOT NULL) "
                    "RETURNING id",
                    (
                        AGENT_LEASE_TTL_S,
                        incarnation.agent_id,
                        incarnation.generation,
                        incarnation.owner,
                        str(incarnation.generation),
                        str(incarnation.owner),
                    ),
                )
                if await cursor.fetchone() is None:
                    raise RuntimeOwnershipLostError(
                        f"agent {incarnation.agent_id} lost authority during database recovery"
                    )
    except TimeoutError as exc:
        # Bound both pool acquisition and a half-open connection/row-lock wait.
        # External Task.cancel remains CancelledError and is never translated.
        raise PoolTimeout("host database recovery probe timed out") from exc


async def recover_database(
    *,
    pool: AsyncConnectionPool,
    checkpointer: AsyncPostgresSaver,
    graph: CompiledStateGraph[Any, Any, Any, Any],
    incarnation: RuntimeIncarnation,
) -> None:
    """Recover inside the original single-flight task, without an inbound wake.

    Cancellation interrupts both probe and backoff. A database flap retries the
    same repair; ownership loss and non-database failures escape to the host's
    existing failure/maintenance fence. No lifecycle receipt is produced here.
    """
    if current_incarnation(incarnation.agent_id) != incarnation:
        raise RuntimeOwnershipLostError("database recovery needs the original bound incarnation")
    backoff = _INITIAL_BACKOFF_SECONDS
    logger.warning("host turn waiting for database recovery", agent_id=incarnation.agent_id)
    with database_wait(incarnation) as waiting:
        while True:
            waiting.renew()
            try:
                async with asyncio.timeout(_RECOVERY_TIMEOUT_SECONDS):
                    await _refresh_owner(pool, incarnation)
                    # Retained N-step writes are still this task's work. Persist them
                    # before deciding which claimed messages reached the checkpoint.
                    await flush_checkpoint(checkpointer, incarnation.agent_id)
                    await _reconcile_claimed_inbounds_at_startup(
                        pool, checkpointer, incarnation.agent_id
                    )
                    await _refresh_owner(pool, incarnation)
                    await _repair_dangling_tool_use_at_startup(graph, incarnation.agent_id)
                    await _refresh_owner(pool, incarnation)
                waiting.complete()
                logger.info("host turn database recovered", agent_id=incarnation.agent_id)
                return
            except (psycopg.OperationalError, PoolTimeout, TimeoutError):
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF_SECONDS)
