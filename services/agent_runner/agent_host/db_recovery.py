"""Keep an interrupted host turn alive until its database is usable again."""

import asyncio
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import psycopg
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph.state import CompiledStateGraph
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from agent.impersonation import flush_checkpoint
from agent.ownership.inbound import RuntimeOwnershipLostError
from agent.ownership.native_cancel import observe_bound_cancel
from agent.startup import (
    reconcile_claimed_inbounds_at_startup,
    repair_dangling_tool_use_at_startup,
)
from base.agents.history.delta_read_compat import (
    RecoveryReconstructionScope,
    recovery_reconstruction_scope,
)
from base.agents.history.inbound_sideload import ReconcileReadInputs
from base.agents.incarnation.native_work_models import NativeWorkTarget
from base.agents.observation.db_wait import DatabaseWait, DatabaseWaits
from base.config import settings
from base.db.transaction import async_write_transaction
from base.deploy.progress_timeout import AGENT_LEASE_TTL_S
from base.log import logger
from base.native_process.runtime_incarnation import RuntimeIncarnation
from services.agent_runner.agent_host.recovery.interrupt import RecoveryInterrupt

_PROBE_TIMEOUT_SECONDS = 5.0
# The observed checkpoint read/recovery band reaches 25-45s under load, and a
# settle/deliver chain walking the delta history more than once was measured at
# 48.6-55.9s with worst-case estimates of 75-100s (INC-927 / task #4781): one
# settle pass or recovery stage must fit its read, write, flush and receipt.
# 120s keeps the finite one-stage fence (#1972) with that headroom.
_DATABASE_PHASE_TIMEOUT_SECONDS = 120.0
_INITIAL_BACKOFF_SECONDS = 1.0
_MAX_BACKOFF_SECONDS = 30.0


class DatabaseRecoveryBudgetExceededError(RuntimeError):
    """The original turn exhausted its database-recovery ladder budget."""


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


async def _run_bounded_stage(
    waiting: DatabaseWait,
    stage: Callable[[], Awaitable[None]],
    *,
    phase: str,
    agent_id: int,
    attempt: int,
) -> None:
    """Renew the database-wait evidence for one real bounded stage, then run it
    under the shared 120s `database_phase()` bound.

    Renewal happens only when this original task/incarnation actually enters a
    stage — heartbeat snapshot reads (`DatabaseWaits.snapshot`) never renew —
    and every renew keeps the same finite proof TTL. A stage that overruns its
    bound raises `PoolTimeout` (from `database_phase`), which the recovery loop
    already retries on its backoff ladder; external cancellation stays
    `CancelledError` and propagates untouched.
    """
    waiting.renew()
    started = time.monotonic()
    try:
        async with database_phase():
            await stage()
    except (PoolTimeout, TimeoutError) as exc:
        logger.warning(
            "host checkpoint recovery stage timed out",
            agent_id=agent_id,
            attempt=attempt,
            phase=phase,
            duration_ms=(time.monotonic() - started) * 1000,
            outcome="phase_timeout" if isinstance(exc.__cause__, TimeoutError) else "pool_timeout",
            error_type=type(exc).__name__,
        )
        raise
    except Exception as exc:
        logger.warning(
            "host checkpoint recovery stage failed",
            agent_id=agent_id,
            attempt=attempt,
            phase=phase,
            duration_ms=(time.monotonic() - started) * 1000,
            outcome="error",
            error_type=type(exc).__name__,
        )
        raise
    logger.info(
        "host checkpoint recovery stage complete",
        agent_id=agent_id,
        attempt=attempt,
        phase=phase,
        duration_ms=(time.monotonic() - started) * 1000,
        outcome="success",
    )


async def _flush_if_needed(
    waiting: DatabaseWait,
    checkpointer: AsyncPostgresSaver,
    reconstruction: RecoveryReconstructionScope | None,
    agent_id: int,
    attempt: int,
    flushed_generation: int | None,
) -> int | None:
    """Reuse a completed flush only while a wrapped saver's state is unchanged."""
    if reconstruction is not None and flushed_generation == reconstruction.generation:
        logger.info(
            "host checkpoint recovery stage complete",
            agent_id=agent_id,
            attempt=attempt,
            phase="checkpoint_flush",
            duration_ms=0.0,
            outcome="already_flushed",
        )
        return reconstruction.generation
    if reconstruction is not None:
        reconstruction.invalidate()
    await _run_bounded_stage(
        waiting,
        lambda: flush_checkpoint(checkpointer, agent_id),
        phase="checkpoint_flush",
        agent_id=agent_id,
        attempt=attempt,
    )
    return reconstruction.generation if reconstruction is not None else None


def _recovery_readers(
    checkpointer: AsyncPostgresSaver,
    graph: CompiledStateGraph[Any, Any, Any, Any],
    reconstruction: RecoveryReconstructionScope | None,
) -> tuple[AsyncPostgresSaver, CompiledStateGraph[Any, Any, Any, Any]]:
    if reconstruction is None:
        return checkpointer, graph
    reader = reconstruction.reader()
    return reader, graph.copy({"checkpointer": reader})


async def recover_database(
    *,
    pool: AsyncConnectionPool,
    checkpointer: AsyncPostgresSaver,
    graph: CompiledStateGraph[Any, Any, Any, Any],
    incarnation: RuntimeIncarnation,
    database_waits: DatabaseWaits,
    peek_lock: asyncio.Lock,
    work: NativeWorkTarget | None,
    reconcile_inputs: ReconcileReadInputs,
    reconstruction_parent: RecoveryReconstructionScope | None = None,
) -> None:
    """Recover inside the original single-flight task, without an inbound wake.

    A short owner probe precedes independently bounded consistency repair stages.
    Cancellation interrupts both probe and backoff. A database flap retries the
    same repair; ownership loss and non-database failures escape to the host's
    existing failure/maintenance fence. No lifecycle receipt is produced here.
    External interrupt intent shortens one backoff; repair still completes before
    the normal claim applies control, so an unreadable checkpoint cannot be paused
    by falsely acknowledging its pending cancel.

    The total budget is checked before each attempt, outside the retry handler.
    A bounded attempt and its backoff may finish beyond it; no further attempt
    starts once it is spent. Exhaustion uses the existing turn crash path.
    """
    recovery_started = time.monotonic()
    backoff = _INITIAL_BACKOFF_SECONDS
    interrupt = RecoveryInterrupt(pool, incarnation, peek_lock, work=work)
    attempt = 0
    phase = "owner_probe"
    last_error_type: str | None = None
    last_sqlstate: str | None = None
    prolonged = False
    flushed_generation: int | None = None
    logger.warning("host turn waiting for checkpoint recovery", agent_id=incarnation.agent_id)
    with (
        recovery_reconstruction_scope(
            reconstruction_parent.saver if reconstruction_parent is not None else checkpointer,
            str(incarnation.agent_id),
            parent=reconstruction_parent,
        ) as reconstruction,
        database_waits.wait(incarnation) as waiting,
    ):
        checkpointer, graph = _recovery_readers(checkpointer, graph, reconstruction)
        while True:
            started = time.monotonic()
            total_elapsed = started - recovery_started
            if total_elapsed >= settings.daemon.host_db_recovery_budget_seconds:
                logger.error(
                    "host checkpoint recovery abandoned",
                    agent_id=incarnation.agent_id,
                    attempts=attempt,
                    total_elapsed_seconds=total_elapsed,
                    final_phase=phase,
                    last_error_type=last_error_type,
                    last_sqlstate=last_sqlstate,
                )
                raise DatabaseRecoveryBudgetExceededError(
                    f"agent {incarnation.agent_id} exhausted database recovery budget "
                    f"after {attempt} attempts in {total_elapsed:.3f}s"
                )
            attempt += 1
            phase = "owner_probe"
            try:
                # Each DB-only stage carries its own 120s `database_phase()`
                # bound (issue #1972): one slow stage times out alone instead
                # of eating the budget every following stage needs. The
                # exact-owner probe keeps its independent 5s bound.
                await _run_bounded_stage(
                    waiting,
                    lambda: _refresh_owner(pool, incarnation),
                    phase=phase,
                    agent_id=incarnation.agent_id,
                    attempt=attempt,
                )
                # Retained N-step writes are still this task's work. Persist them
                # before deciding which claimed messages reached the checkpoint.
                phase = "checkpoint_flush"
                flushed_generation = await _flush_if_needed(
                    waiting,
                    checkpointer,
                    reconstruction,
                    incarnation.agent_id,
                    attempt,
                    flushed_generation,
                )
                # An accepted strong command belongs to the interrupted original
                # work. The host must settle its exact marker before startup repair
                # can change channels or dispose claimed rows.
                if (
                    await observe_bound_cancel(
                        pool, incarnation.agent_id, incarnation=incarnation, work=work
                    )
                    is not None
                ):
                    waiting.complete()
                    return
                phase = "inbound_reconciliation"
                await _run_bounded_stage(
                    waiting,
                    lambda: reconcile_claimed_inbounds_at_startup(
                        pool,
                        checkpointer,
                        incarnation.agent_id,
                        incarnation=incarnation,
                        inputs=reconcile_inputs,
                    ),
                    phase=phase,
                    agent_id=incarnation.agent_id,
                    attempt=attempt,
                )
                phase = "owner_revalidation"
                await _run_bounded_stage(
                    waiting,
                    lambda: _refresh_owner(pool, incarnation),
                    phase=phase,
                    agent_id=incarnation.agent_id,
                    attempt=attempt,
                )
                phase = "tool_state_repair"
                await _run_bounded_stage(
                    waiting,
                    lambda: repair_dangling_tool_use_at_startup(graph, incarnation.agent_id),
                    phase=phase,
                    agent_id=incarnation.agent_id,
                    attempt=attempt,
                )
                phase = "repaired_owner_validation"
                await _run_bounded_stage(
                    waiting,
                    lambda: _refresh_owner(pool, incarnation),
                    phase=phase,
                    agent_id=incarnation.agent_id,
                    attempt=attempt,
                )
                waiting.complete()
                completed = time.monotonic()
                logger.info(
                    "host turn checkpoint recovered",
                    agent_id=incarnation.agent_id,
                    attempt=attempt,
                    elapsed_seconds=completed - started,
                    total_attempts=attempt,
                    total_elapsed_seconds=completed - recovery_started,
                )
                return
            except (psycopg.OperationalError, PoolTimeout, TimeoutError) as exc:
                failed = time.monotonic()
                total_elapsed = failed - recovery_started
                last_error_type = type(exc).__name__
                last_sqlstate = exc.sqlstate if isinstance(exc, psycopg.Error) else None
                logger.warning(
                    "host checkpoint recovery retry",
                    agent_id=incarnation.agent_id,
                    attempt=attempt,
                    phase=phase,
                    elapsed_seconds=failed - started,
                    error_type=last_error_type,
                    sqlstate=last_sqlstate,
                    backoff_seconds=backoff,
                )
                if not prolonged and (
                    attempt >= settings.daemon.host_db_recovery_prolonged_attempts
                    or total_elapsed >= settings.daemon.host_db_recovery_prolonged_seconds
                ):
                    prolonged = True
                    logger.warning(
                        "host checkpoint recovery prolonged",
                        agent_id=incarnation.agent_id,
                        attempts=attempt,
                        total_elapsed_seconds=total_elapsed,
                        phase=phase,
                        error_type=last_error_type,
                        sqlstate=last_sqlstate,
                    )
                await interrupt.wait_backoff(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF_SECONDS)
