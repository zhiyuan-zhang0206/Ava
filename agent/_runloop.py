"""Shared graph invocation configuration and recoverable turn-error reporting."""

from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph

from agent.hooks.compact import CompactionFailedError
from agent.state import BaseAgentState
from shared.audit_events import insert_event_log_async
from shared.live_events import Error
from shared.log import logger

from .graph._context import AvaContext
from .graph._llm import FatalLLMStreamError, FatalProviderError
from .state_channels import (
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

# LangGraph recursion_limit defaults to 25 — far too low for this graph even
# per-turn: one invocation is one TURN, and a turn is a whole work bout (the
# claim → llm → exec cycle repeats until the agent halts), which can run for
# days. `int` has no inf; 2**31-1 keeps the rail effectively unbounded. The
# spec calls this `recursion_limit=∞` ("widest possible runaway protection
# rail").
_RECURSION_LIMIT_INF = 2**31 - 1


def _emit_error_event(
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
) -> None:
    """Best-effort single Error event to the frontend SSE channel.

    emit() is a non-blocking best-effort enqueue; the suppress guards the
    assert so a missing publisher can't mask the original exception.
    """
    with suppress(Exception):
        assert ctx.event_publisher is not None  # noqa: S101
        ctx.event_publisher.emit(
            Error(
                agent_id=agent_id,
                content=content,
                error_class=error_class,
                provider=provider,
                status=status,
                reason=reason,
                blocked=blocked,
                recovery=recovery,
            ).model_dump_json()
        )


def _circuit_reason(exc: FatalProviderError) -> str:
    """Map a permanent provider rejection to its circuit-breaker reason.

    The one reason that matters for behaviour is `context_overflow` — the
    self-rescuable case the forced-compact arm keys on. Every other rejection
    (auth / billing / schema / ...) just stops heartbeat re-fires until a
    real wake succeeds; they map to a stable label for the breaker event,
    with a generic `permanent` fallback for anything unmapped."""
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


async def _handle_fatal_llm_error(
    exc: FatalLLMStreamError | FatalProviderError,
    ctx: AvaContext,
    agent_id: int,
    circuit_reader: Callable[[], Awaitable[CircuitState | None]] | None = None,
    *,
    occurred_at: datetime | None = None,
    emit_reports: bool = True,
) -> dict[str, object]:
    """Fatal LLM turn abort: log + one Error event, return the fresh-run input.

    The retry cap fired, or the provider permanently rejected the request
    (402/401/403) — abort this turn but keep the agent alive. One Error event
    to the frontend, traceback into the file sink, then re-invoke with a fresh
    run: claim node idles for the next inbound so the user can see the error
    and the agent can recover on a later user-directed wake once the cause
    clears (e.g. balance topped up) without a manual revive.

    A `FatalProviderError` additionally OPENS the heartbeat circuit breaker:
    the next heartbeat nudge would re-fire the same doomed call (the 3962
    incident — 80 heartbeat cycles against a permanent context-overflow 400).
    The breaker state rides the fresh-run input (`circuit` channel); while it
    is open the claim consumes heartbeat check-ins without routing to the
    LLM, and for the `context_overflow` reason routes any wake into a forced
    compaction instead. It closes on the first successful LLM call (llm_node).
    A non-overflow permanent rejection additionally sends a metadata-only
    report to the nearest alive immutable-SPAWN ancestor; it never replays the
    rejected provider text or conversation history.

    A retry after interrupted preparation sets ``emit_reports=False``: it
    recomputes the state update without repeating best-effort notifications.
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
        isinstance(exc, FatalProviderError)
        and exc.error_class == "permanent"
        and reason != CIRCUIT_REASON_CONTEXT_OVERFLOW
    )
    content = (
        f"{type(exc).__name__}: {exc} The agent is blocked; heartbeat check-ins "
        "will not re-run this request. Resolve the cause, then send a new message."
        if is_blocked_provider_failure
        else f"{type(exc).__name__}: {exc} The turn was aborted; the agent "
        "is still alive and idling. It retries on the next message or "
        "wake-up once the underlying cause is resolved."
    )
    if emit_reports:
        _emit_error_event(
            ctx,
            agent_id,
            content,
            error_class=getattr(exc, "error_class", None),
            provider=getattr(exc, "provider", None),
            status=getattr(exc, "status", None),
            reason=reason,
            blocked=is_blocked_provider_failure,
            recovery=_provider_recovery(reason) if reason is not None else None,
        )
    # halted=True so the claim node actually waits for the next inbound
    # instead of re-entering the LLM with the same messages and hitting the
    # same fatal error in an infinite loop (agent #1581 incident — DeepSeek
    # stream stall with 616 messages).
    input_update: dict[str, object] = {"halted": True}
    if isinstance(exc, FatalProviderError):
        assert reason is not None  # noqa: S101
        already_open = False
        if circuit_reader is not None:
            try:
                current = await circuit_reader()
            except Exception:
                current = None
            already_open = current is not None and current.open and current.reason == reason
        if already_open:
            # The breaker is still open for the same reason from an earlier
            # failed turn — re-opening is idempotent and re-emitting the open
            # event per failed wake is noise (QA #903 nit). Skip both; the
            # original opened_at is preserved.
            logger.info(
                "heartbeat circuit breaker already open (reason={reason}) — "
                "skipping duplicate open",
                event="circuit_breaker_open",
                agent_id=agent_id,
                reason=reason,
                status=exc.status,
            )
            return input_update
        input_update["circuit"] = CircuitState(
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
        # Record the breaker-open event in event_log (best-effort: a failure
        # only loses the audit row, never the breaker state itself).
        if emit_reports and ctx.ops_pool is not None:
            try:
                await insert_event_log_async(
                    event_type="circuit_breaker",
                    agent_id=agent_id,
                    source="system",
                    payload={
                        "action": "open",
                        "reason": reason,
                        "status": exc.status,
                        "error_class": exc.error_class,
                    },
                )
            except Exception as exc_log:
                logger.warning(
                    "failed to record circuit_breaker event: {exc!r}",
                    agent_id=agent_id,
                    exc=exc_log,
                )
        if emit_reports and ctx.ops_pool is not None and is_blocked_provider_failure:
            from agent.db import enqueue_fatal_provider_report_to_nearest_alive_ancestor

            assert exc.error_class is not None  # noqa: S101
            try:
                await enqueue_fatal_provider_report_to_nearest_alive_ancestor(
                    ctx.ops_pool,
                    agent_id,
                    error_class=exc.error_class,
                    provider=exc.provider,
                    status=exc.status,
                    reason=reason,
                    occurred_at=occurred_at if occurred_at is not None else datetime.now(UTC),
                )
            except Exception as exc_report:
                logger.warning(
                    "failed to enqueue fatal provider report to an ancestor: {exc!r}",
                    agent_id=agent_id,
                    exc=exc_report,
                )
    return input_update


def _graph_config(agent_id: int, tags: list[str], metadata: dict[str, object]) -> RunnableConfig:
    """LangGraph invoke config: thread_id + infinite recursion limit + the
    trace fields (run_name / metadata / tags) for backend filtering."""
    return {
        "configurable": {"thread_id": str(agent_id)},
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


async def settle_turn_failure(
    graph: CompiledStateGraph[BaseAgentState, AvaContext, BaseAgentState, BaseAgentState],
    checkpointer: object,
    config: RunnableConfig,
    ctx: AvaContext,
    agent_id: int,
    pending: PendingTurnFailure,
) -> None:
    """Prepare notifications once, then retry only the abort's durable writes."""

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
            _emit_error_event(
                ctx,
                agent_id,
                f"CompactionFailedError: {exc} The turn was aborted and conversation history "
                "was preserved. Send a new message to continue, or request compaction again "
                "after resolving the cause.",
            )
            pending.prepared_update = {"halted": True}
        else:
            pending.prepared_update = await _handle_fatal_llm_error(
                exc, ctx, agent_id, circuit_reader=read_circuit, emit_reports=emit_reports
            )
    await graph.aupdate_state(config, pending.prepared_update)
    from agent.impersonation import flush_checkpoint

    await flush_checkpoint(checkpointer, agent_id)
