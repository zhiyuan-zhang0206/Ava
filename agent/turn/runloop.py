"""Shared graph invocation configuration and recoverable turn-error reporting."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph

from agent.graph.llm_errors import FatalLLMStreamError, FatalProviderError
from agent.hooks.compact import CompactionFailedError
from agent.startup import CHECKPOINT_INTERVAL_KEY
from agent.state import BaseAgentState
from agent.state_channels import (
    CIRCUIT_REASON_AUTH,
    CIRCUIT_REASON_BAD_REQUEST,
    CIRCUIT_REASON_BILLING,
    CIRCUIT_REASON_CONTEXT_OVERFLOW,
    CIRCUIT_REASON_FORBIDDEN,
    CIRCUIT_REASON_MODEL_NOT_FOUND,
    CIRCUIT_REASON_PERMANENT,
    CIRCUIT_REASON_SCHEMA,
    CircuitState,
)
from base.agents.context import AvaContext
from base.events.live.bus import EventBus
from base.events.live.projection import Error
from base.log import logger
from base.telemetry.audit_events import prepare_event_log, record_audit_reported_async

# LangGraph recursion_limit defaults to 25 — far too low for this graph even
# per-turn: one invocation is one TURN, and a turn is a whole work bout (the
# claim → llm → exec cycle repeats until the agent halts), which can run for
# days. `int` has no inf; 2**31-1 keeps the rail effectively unbounded. The
# spec calls this `recursion_limit=∞` ("widest possible runaway protection
# rail").
_RECURSION_LIMIT_INF = 2**31 - 1


def emit_error_event(
    ctx: AvaContext,
    agent_id: int,
    content: str,
    *,
    error_class: str | None = None,
    provider: str | None = None,
    status: int | None = None,
    reason: str | None = None,
    blocked: bool = False,
    recovery: str | None = None,
    deferred: list[Error] | None = None,
) -> None:
    """Best-effort single Error event to the frontend SSE channel.

    A context built without a publisher (an embedding caller) has no channel, so the event
    is dropped; a failing emit is logged, never raised over the original exception.
    Hosted failures defer the same typed payload until their durable settlement.
    """
    publisher = ctx.event_publisher
    if publisher is None:
        return
    try:
        error = Error(
            agent_id=agent_id,
            content=content,
            error_class=error_class,
            provider=provider,
            status=status,
            reason=reason,
            blocked=blocked,
            recovery=recovery,
        )
        if deferred is not None:
            deferred.append(error)
        else:
            publisher.emit(error.model_dump_json())
    except Exception:
        # Reporting must not mask the original exception.
        logger.opt(exception=True).warning(
            "emitting the turn Error event for agent {} failed; the frontend does not see it",
            agent_id,
        )


def _circuit_reason(exc: FatalProviderError) -> str:
    """Map a permanent provider rejection to its circuit-breaker reason.

    Reasons classify the required user action; every permanent rejection
    suppresses background wakes until corrected.
    """
    if exc.context_overflow:
        return CIRCUIT_REASON_CONTEXT_OVERFLOW
    status = exc.status
    if status is None:
        return CIRCUIT_REASON_PERMANENT
    return {
        401: CIRCUIT_REASON_AUTH,
        402: CIRCUIT_REASON_BILLING,
        403: CIRCUIT_REASON_FORBIDDEN,
        404: CIRCUIT_REASON_MODEL_NOT_FOUND,
        422: CIRCUIT_REASON_SCHEMA,
        400: CIRCUIT_REASON_BAD_REQUEST,
    }.get(status, CIRCUIT_REASON_PERMANENT)


def _provider_recovery(reason: str) -> str:
    """Return the user action for a blocked permanent provider rejection."""
    if reason == CIRCUIT_REASON_CONTEXT_OVERFLOW:
        return "Compact the conversation, then send a new message."
    if reason in (CIRCUIT_REASON_AUTH, CIRCUIT_REASON_BILLING, CIRCUIT_REASON_FORBIDDEN):
        return "Resolve the provider credentials or billing issue, then send a new message."
    if reason == CIRCUIT_REASON_MODEL_NOT_FOUND:
        return "Choose a different model overlay, then send a new message."
    if reason == CIRCUIT_REASON_SCHEMA:
        return "Correct the request or model configuration, then send a new message."
    return "Choose a different model overlay or resolve the provider policy rejection, then send a new message."


def _model_vendor(ctx: AvaContext) -> str | None:
    """Vendor key of this turn's model — the account a failure bills to.

    The classifier stamps the same read onto the ``llm_provider_error`` log's
    ``vendor`` field (`agent/graph/llm_errors.py`), so the human-facing
    reports and the machine event name the same account: a DeepSeek balance
    rejection stays ``vendor=deepseek`` even when it surfaced through the
    anthropic-compat path (``provider=anthropic`` — the 2026-09-18 billing
    wave, task #3916). ``None`` for an unregistered model prefix.
    """
    from base.lm.factory import provider_key_of_model

    return provider_key_of_model(ctx.require_agent().brain.llm_model, catalog=ctx.require_catalog())


async def _record_permanent_reject_outcome(
    ctx: AvaContext,
    agent_id: int,
    exc: FatalProviderError,
    reason: str,
    occurred_at: datetime | None,
    deferred: list[Error] | None,
) -> None:
    """Count one permanent-class rejection; at the halt threshold trip the breaker.

    `base/agents/recovery/breaker.py` owns the semantics: one permanent rejection stops background wakes until fresh explicit input
    permits another attempt (task #3617, design #3610 section 12).
    Every halted rejection re-runs the escalation — with automatic recovery
    halted, a further rejected turn was a real claimed inbound (a human or a
    delivery), and deserves one honest escalation; the state writes are
    idempotent. All of it is best-effort: none may mask the provider rejection
    that aborted the turn.
    """
    from base.agents.recovery.breaker import (
        HALT_AFTER_CONSECUTIVE_PERMANENT_REJECTS,
        halt_automatic_recovery,
        record_permanent_reject_turn,
    )

    assert ctx.ops_pool is not None  # noqa: S101
    try:
        streak = await record_permanent_reject_turn(ctx.ops_pool, agent_id, reason)
    except Exception:
        logger.opt(exception=True).warning(
            "failed to record a permanent-rejection streak for agent {agent_id}; "
            "the recovery circuit breaker is not updated",
            agent_id=agent_id,
        )
        return
    if streak < HALT_AFTER_CONSECUTIVE_PERMANENT_REJECTS:
        return
    try:
        freshly_tripped = await halt_automatic_recovery(ctx.ops_pool, agent_id)
    except Exception:
        freshly_tripped = False
        logger.opt(exception=True).warning(
            "failed to suppress automatic wakes for halted agent {agent_id}",
            agent_id=agent_id,
        )
    if freshly_tripped:
        logger.warning(
            "recovery circuit breaker tripped for agent {agent_id} — automatic "
            "recovery halted after {streak} consecutive permanent provider "
            "rejections (reason={reason})",
            event="recovery_breaker_halt",
            agent_id=agent_id,
            streak=streak,
            reason=reason,
        )
        # The halt itself is already in force; a failed audit write is reported
        # (`audit_write_failed`) instead of aborting the rest of the handling.
        await record_audit_reported_async(
            ctx.ops_pool,
            prepare_event_log(
                event_type="circuit_breaker",
                agent_id=agent_id,
                source="system",
                payload={
                    "action": "halt",
                    "reason": reason,
                    "status": exc.status,
                    "error_class": exc.error_class,
                    "streak": streak,
                },
            ),
        )
    try:
        from agent.db import enqueue_fatal_provider_report_to_nearest_alive_ancestor

        await enqueue_fatal_provider_report_to_nearest_alive_ancestor(
            ctx.ops_pool,
            agent_id,
            db=ctx.require_db(),
            bus=ctx.require_bus(),
            error_class=exc.error_class or "permanent",
            provider=exc.provider,
            vendor=_model_vendor(ctx),
            status=exc.status,
            reason=reason,
            occurred_at=occurred_at if occurred_at is not None else datetime.now(UTC),
        )
    except Exception:
        logger.opt(exception=True).warning(
            "failed to enqueue the recovery-halt report to an ancestor",
            agent_id=agent_id,
        )
    emit_error_event(
        ctx,
        agent_id,
        f"{type(exc).__name__}: {exc} Automatic recovery is halted after "
        f"{streak} consecutive permanent provider rejections. Resolve the cause, "
        "then send a new message to try again.",
        error_class=exc.error_class,
        provider=exc.provider,
        status=exc.status,
        reason=reason,
        blocked=True,
        recovery="Resolve the provider rejection, then send a new message.",
        deferred=deferred,
    )


def _abort_message(exc: FatalLLMStreamError | FatalProviderError, *, blocked: bool) -> str:
    if blocked:
        return (
            f"{type(exc).__name__}: {exc} The agent is blocked; heartbeat check-ins "
            "will not re-run this request. Resolve the cause, then send a new message."
        )
    return (
        f"{type(exc).__name__}: {exc} The turn was aborted; the agent "
        "is still alive and idling. It retries on the next message or "
        "wake-up once the underlying cause is resolved."
    )


async def _breaker_already_open(
    circuit_reader: Callable[[], Awaitable[CircuitState | None]] | None, reason: str
) -> bool:
    if circuit_reader is None:
        return False
    try:
        current = await circuit_reader()
    except Exception:
        logger.opt(exception=True).warning(
            "reading the circuit breaker state failed; treating the breaker as not open"
        )
        current = None
    return current is not None and current.open and current.reason == reason


async def _open_breaker(
    ctx: AvaContext,
    agent_id: int,
    exc: FatalProviderError,
    reason: str,
    *,
    emit_reports: bool,
) -> CircuitState:
    """Open the heartbeat circuit breaker for `reason`, with its log line and audit event."""
    state = CircuitState(
        open=True,
        reason=reason,
        opened_at=datetime.now(UTC).isoformat(),
    )
    logger.warning(
        "heartbeat circuit breaker OPEN — reason={reason} status={status}",
        event="circuit_breaker_open",
        agent_id=agent_id,
        reason=reason,
        status=exc.status,
    )
    # Record the breaker-open event as an audit event. The breaker state is
    # already set; a failed audit write is reported (`audit_write_failed`),
    # never allowed to undo or skip the rest of the handling.
    if emit_reports and ctx.ops_pool is not None:
        await record_audit_reported_async(
            ctx.ops_pool,
            prepare_event_log(
                event_type="circuit_breaker",
                agent_id=agent_id,
                source="system",
                payload={
                    "action": "open",
                    "reason": reason,
                    "status": exc.status,
                    "error_class": exc.error_class,
                },
            ),
        )
    return state


async def _apply_provider_failure(
    input_update: dict[str, object],
    ctx: AvaContext,
    agent_id: int,
    exc: FatalProviderError,
    reason: str,
    circuit_reader: Callable[[], Awaitable[CircuitState | None]] | None,
    *,
    occurred_at: datetime | None,
    emit_reports: bool,
    deferred: list[Error] | None,
) -> None:
    """Record a permanent rejection and open the heartbeat circuit breaker into `input_update`."""
    if emit_reports and ctx.ops_pool is not None and exc.error_class == "permanent":
        # Persist the halt even when the checkpoint circuit was already open.
        await _record_permanent_reject_outcome(ctx, agent_id, exc, reason, occurred_at, deferred)
    if await _breaker_already_open(circuit_reader, reason):
        # The breaker is still open for the same reason from an earlier
        # failed turn — re-opening is idempotent and re-emitting the open
        # event per failed wake is noise (QA #903 nit). Skip both; the
        # original opened_at is preserved.
        logger.info(
            "heartbeat circuit breaker already open (reason={reason}) — skipping duplicate open",
            event="circuit_breaker_open",
            agent_id=agent_id,
            reason=reason,
            status=exc.status,
        )
        return
    input_update["circuit"] = await _open_breaker(
        ctx, agent_id, exc, reason, emit_reports=emit_reports
    )


async def _handle_fatal_llm_error(
    exc: FatalLLMStreamError | FatalProviderError,
    ctx: AvaContext,
    agent_id: int,
    circuit_reader: Callable[[], Awaitable[CircuitState | None]] | None = None,
    *,
    occurred_at: datetime | None = None,
    emit_reports: bool = True,
    deferred: list[Error] | None = None,
) -> dict[str, object]:
    """Abort the turn, report the error, and wait for new input.

    A permanent provider rejection also persists a halt for background wakes and
    opens the checkpoint heartbeat circuit. A fresh explicit chat may retry after
    the underlying cause is fixed; original code execution is not resumed.
    ``emit_reports=False`` computes state without repeating notifications.
    """
    logger.opt(exception=True).error(
        "LLM turn aborted (retry cap exhausted or provider rejected) — "
        "agent stays alive and idles for next inbound",
        event="llm_turn_aborted",
        # FatalProviderError carries the classifier's structured triple;
        # FatalLLMStreamError does not (getattr -> None) — keeps the abort
        # event queryable by error_class / provider / status either way.
        error_class=getattr(exc, "error_class", None),
        provider=getattr(exc, "provider", None),
        status=getattr(exc, "status", None),
        context_overflow=getattr(exc, "context_overflow", None),
    )
    reason = _circuit_reason(exc) if isinstance(exc, FatalProviderError) else None
    is_blocked_provider_failure = (
        isinstance(exc, FatalProviderError) and exc.error_class == "permanent"
    )
    content = _abort_message(exc, blocked=is_blocked_provider_failure)
    if emit_reports:
        emit_error_event(
            ctx,
            agent_id,
            content,
            error_class=getattr(exc, "error_class", None),
            provider=getattr(exc, "provider", None),
            status=getattr(exc, "status", None),
            reason=reason,
            blocked=is_blocked_provider_failure,
            recovery=_provider_recovery(reason) if reason is not None else None,
            deferred=deferred,
        )
    # halted=True so the claim node actually waits for the next inbound
    # instead of re-entering the LLM with the same messages and hitting the
    # same fatal error in an infinite loop (agent #1581 incident — DeepSeek
    # stream stall with 616 messages).
    input_update: dict[str, object] = {"halted": True}
    if isinstance(exc, FatalProviderError):
        assert reason is not None  # noqa: S101
        await _apply_provider_failure(
            input_update,
            ctx,
            agent_id,
            exc,
            reason,
            circuit_reader,
            occurred_at=occurred_at,
            emit_reports=emit_reports,
            deferred=deferred,
        )
    return input_update


def graph_config(
    agent_id: int, tags: list[str], metadata: dict[str, object], checkpoint_interval: int
) -> RunnableConfig:
    """LangGraph invoke config: thread_id + the turn's checkpoint interval + infinite recursion
    limit + the trace fields (run_name / metadata / tags) for backend filtering."""
    return {
        "configurable": {"thread_id": str(agent_id), CHECKPOINT_INTERVAL_KEY: checkpoint_interval},
        "recursion_limit": _RECURSION_LIMIT_INF,
        "run_name": f"ava-agent-{agent_id}",
        "metadata": metadata,
        "tags": tags,
    }


@dataclass
class PendingTurnFailure:
    """A single turn's prepared abort survives database-only settlement retries."""

    error: FatalLLMStreamError | FatalProviderError | CompactionFailedError
    prepared_update: dict[str, object] | None = None
    reports_started: bool = False
    errors: list[Error] = field(default_factory=list[Error])


async def settle_turn_failure(
    graph: CompiledStateGraph[BaseAgentState, AvaContext, BaseAgentState, BaseAgentState],
    checkpointer: object,
    config: RunnableConfig,
    ctx: AvaContext,
    agent_id: int,
    pending: PendingTurnFailure,
) -> None:
    """Prepare the abort once; its host publishes errors after durable turn settlement."""

    async def read_circuit() -> CircuitState | None:
        snapshot = await graph.aget_state(config)
        value = snapshot.values.get("circuit")
        return CircuitState.model_validate(value) if value is not None else None

    if pending.prepared_update is None:
        # A database-phase timeout can interrupt preparation after a report
        # committed. Retrying the state calculation must not repeat that wake.
        emit_reports = not pending.reports_started
        pending.reports_started = True
        exc = pending.error
        if isinstance(exc, CompactionFailedError):
            logger.warning(
                "compaction failed — aborting turn, preserving history for the next inbound",
                event="compact_turn_aborted",
                agent_id=agent_id,
            )
            emit_error_event(
                ctx,
                agent_id,
                f"CompactionFailedError: {exc} The turn was aborted and conversation history "
                "was preserved. Send a new message to continue, or request compaction again "
                "after resolving the cause.",
                deferred=pending.errors,
            )
            pending.prepared_update = {"halted": True}
        else:
            pending.prepared_update = await _handle_fatal_llm_error(
                exc,
                ctx,
                agent_id,
                circuit_reader=read_circuit,
                emit_reports=emit_reports,
                deferred=pending.errors,
            )
    await graph.aupdate_state(config, pending.prepared_update)
    from agent.impersonation_handoff import flush_checkpoint

    await flush_checkpoint(checkpointer, agent_id)


async def publish_turn_failure(pending: PendingTurnFailure, bus: EventBus) -> None:
    """Deliver the prepared live projection after the host proves its fatal cutoff.

    The invocation publisher has already drained and closed. This uses the same
    owned bus and its finite best-effort transport without restarting a worker.
    Consume the local reports before awaiting: a retry or cancellation must not
    duplicate a notification whose transport outcome is unknown.
    """
    errors, pending.errors = pending.errors, []
    for error in errors:
        try:
            delivered = await bus.publish_best_effort(
                error.model_dump_json(), context="settled turn Error"
            )
            if delivered is None:
                logger.warning("settled turn Error for agent {} was not delivered", error.agent_id)
        except Exception:
            logger.opt(exception=True).warning(
                "publishing the settled turn Error for agent {} failed", error.agent_id
            )
