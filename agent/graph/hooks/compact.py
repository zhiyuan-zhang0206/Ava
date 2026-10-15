__description__ = "Auto-compact history when token count exceeds threshold"

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from langchain_core.messages.modifier import RemoveMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.runtime import Runtime
from psycopg_pool import AsyncConnectionPool

from agent.graph.interrupt import ModelInterruptedError, interruptible_model, subscribe_interrupt
from agent.graph.prompt.compaction import compact_contract
from agent.hooks import Hook
from agent.hooks.compact_anchor import SummaryText, closing_of, closing_request_of
from agent.hooks.compact_anchor import compose_summary_message as compose_summary_message
from agent.hooks.compact_events import emit_compact_finished, emit_compact_started
from agent.hooks.history_dump import dump_history, history_dump_note
from agent.hooks.understanding_chunks import await_snapshot, enqueue_closing_chunk
from agent.llm.invoke import ainvoke_tool_call
from agent.messages import (
    NoteTag,
    system_note_message,
)
from agent.nodes import CLAIM, INIT_CONTEXT, NodeName
from agent.state import AgentState, CompactState, ContextReset
from base.agents.context import AvaContext, agent_id_from_config
from base.agents.history.closing_request import ClosingRequest
from base.agents.history.compact_boundary import mark_compact_boundary
from base.agents.messages.kwargs import AvaMsgType, read_ava_kwargs
from base.agents.to_user import DOCUMENT_FILENAME
from base.events.live.projection import Cancelled, CompactDone, CompactionMode, CompactionStatus
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.lm.context_budget import latest_input_tokens, resolve_context_budget
from base.lm.errors import is_retryable_provider_error
from base.log import logger
from base.packages.plugins import activation
from base.packages.plugins.extensions import ExtensionRegistry
from base.telemetry.audit_events import prepare_event_log, record_audit_reported_async

COMPACTION_INSTRUCTION = """[system] Context compaction checkpoint: the conversation above is about to be
replaced by the summary you write now. Produce it now as plain text — do not
write code or call any tool; anything other than text is discarded. Write it
exactly as the `ava.self.compact` contract below specifies: first person, every
section filled, "(none)" only when one genuinely has nothing. Refer to others as
"the user" or "agent N". If a summary already appears above, rewrite it in place
— one flat, updated summary, never a summary nested inside a summary.
"""


COMPACT_MIN_SUMMARY_CHARS = 1000
COMPACT_MAX_ATTEMPTS = 3


class CompactionFailedError(RuntimeError):
    pass


class EmptyCompactionSummaryError(RuntimeError):
    pass


# ava:ref agent/graph/docs/context-window/replacement.ava.okf.md#retained-boundary-checkpoints
async def stamp_compact_boundary(
    pool: AsyncConnectionPool | None,
    agent_id: int,
    state: AgentState | None = None,
    closing: ClosingRequest | None = None,
    *,
    read_agent: Callable[[str], Any],
) -> None:
    if pool is None:
        return
    boundary: str | None = None
    await await_snapshot(pool, state, agent_id, read_agent=read_agent)
    try:
        boundary = await mark_compact_boundary(pool, str(agent_id), closing=closing)
    except Exception as exc:
        logger.warning(
            "[{label}] {body}",
            label="compact-boundary",
            event="compact_boundary_stamp",
            body=f"compact boundary stamp failed for agent {agent_id}: {exc!r}",
        )
    if state is not None:
        await enqueue_closing_chunk(
            state.compact,
            list(state.messages),
            pool=pool,
            agent_id=agent_id,
            boundary=boundary,
            read_agent=read_agent,
        )


def conversation_messages(messages: list[AnyMessage]) -> list[AnyMessage]:
    has_system = bool(messages) and isinstance(messages[0], SystemMessage)
    return messages[1:] if has_system else messages


# ava:ref agent/graph/docs/context-window/preparation.ava.okf.md#completed-window-attribution
def emit_compaction_monitoring(
    messages: list[AnyMessage],
    summary: str,
    *,
    agent_id: int,
    compact_kind: str,
    compact: CompactState,
) -> None:
    history_chars = sum(
        len(message.content) if isinstance(message.content, str) else len(str(message.content))
        for message in conversation_messages(messages)
    )
    summary_chars = len(summary)
    summary_history_ratio = summary_chars / history_chars if history_chars else None
    logger.info(
        "compaction completed: {history_chars} history chars -> {summary_chars} summary chars",
        event="compaction_completed",
        agent_id=agent_id,
        compact_kind=compact_kind,
        reminder_shown=(
            compact.reminder_shown and compact.reminder_seen_version == compact.version
        ),
        compactions=1,
        history_chars=history_chars,
        summary_chars=summary_chars,
        summary_history_ratio=summary_history_ratio,
    )


# ava:ref agent/graph/docs/context-window/replacement.ava.okf.md#summary-request-prefix
async def generate_summary(
    messages: list[AnyMessage],
    llm: BaseChatModel,
    slices: AgentSlices,
    *,
    catalog: ModelCatalog,
) -> SummaryText:
    has_system = bool(messages) and isinstance(messages[0], SystemMessage)
    system_head = messages[:1] if has_system else []
    content_msgs = messages[1:] if has_system else messages

    if not content_msgs:
        raise ValueError("conversation is empty, nothing to compress")

    instruction = HumanMessage(
        content=f"{COMPACTION_INSTRUCTION}\nava.self.compact contract:\n{compact_contract()}"
    )
    compaction_input = [*system_head, *content_msgs, instruction]
    response = await ainvoke_tool_call(
        llm, compaction_input, read_timeout=lambda: slices.read("lm", "llm_compact_timeout_seconds")
    )
    model = slices.brain.llm_model
    if isinstance(model, str) and model:
        from base.lm.usage import log_usage_from_message

        log_usage_from_message(
            response,
            model=model,
            usage_kind="agent",
            catalog=catalog,
        )
    summary = response.text
    closing = closing_request_of(response, instruction)
    if not summary.strip():
        raise EmptyCompactionSummaryError(
            f"Compaction LLM returned no text content"
            f" (summarizing {len(content_msgs)} messages,"
            f" response content type {type(response.content).__name__});"
            f" cannot replace history with an empty summary"
        )
    return SummaryText(summary, closing)


def _estimate_tokens(messages: list[AnyMessage]) -> int:
    total_chars = sum(
        len(m.content) if isinstance(m.content, str) else len(str(m.content)) for m in messages
    )
    return total_chars // 4


# ava:ref agent/graph/docs/context-window/preparation.ava.okf.md#occupancy-and-reply-opportunity
def _context_occupancy(messages: list[AnyMessage]) -> int:
    tokens = latest_input_tokens(messages)
    return tokens if tokens is not None else _estimate_tokens(messages)


# ava:ref agent/graph/docs/context-window/preparation.ava.okf.md#occupancy-and-reply-opportunity
def _summary_awaits_reply(messages: list[AnyMessage]) -> bool:
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            return False
        if isinstance(message, HumanMessage) and read_ava_kwargs(message).get("ava_msg_type") in (
            AvaMsgType.COMPACT_SUMMARY.value,
            AvaMsgType.COMPACT_REQUEST.value,
        ):
            return True
    return False


def auto_compact_will_fire(state: AgentState, agent: AgentSlices, *, catalog: ModelCatalog) -> bool:
    if _summary_awaits_reply(state.messages):
        return False
    budget = resolve_context_budget(agent.brain.llm_model, agent.overrides, catalog=catalog)
    if _context_occupancy(state.messages) <= budget.hard_compact_tokens:
        return False
    return bool(conversation_messages(state.messages))


# ava:ref agent/graph/docs/context-window/replacement.ava.okf.md#compact-failure-boundary
async def auto_compact_summary(
    messages: list[AnyMessage],
    llm: BaseChatModel,
    content_count: int,
    slices: AgentSlices,
    *,
    catalog: ModelCatalog,
) -> str:
    summary: str = ""
    last_error: Exception | None = None
    for attempt in range(1, COMPACT_MAX_ATTEMPTS + 1):
        try:
            summary = await generate_summary(messages, llm, slices, catalog=catalog)
        except Exception as e:
            if not isinstance(e, EmptyCompactionSummaryError) and not is_retryable_provider_error(
                e
            ):
                raise
            last_error = e
            logger.warning(
                "[{label}] {body}",
                label="auto-compact",
                event="auto_compact",
                body=f"attempt {attempt}/{COMPACT_MAX_ATTEMPTS}: {e}; retrying",
            )
            continue
        logger.info(
            "[{label}] {body}",
            label="auto-compact",
            event="auto_compact",
            body=f"attempt {attempt}/{COMPACT_MAX_ATTEMPTS}: summary {len(summary)} chars",
        )
        if len(summary) >= COMPACT_MIN_SUMMARY_CHARS:
            break
        last_error = None
    else:
        detail = f"error: {last_error}" if last_error else f"{len(summary)} chars"
        raise CompactionFailedError(
            f"Compaction produced no usable summary across {COMPACT_MAX_ATTEMPTS}"
            f" attempts (last: {detail}); the model is not following the"
            f" compaction template — refusing to overwrite {content_count} messages"
            f" with a non-summary"
        ) from last_error

    return summary


# ava:ref agent/graph/docs/context-window/replacement.ava.okf.md#compact-failure-boundary
async def auto_compact_for_llm(
    state: AgentState,
    runtime: Runtime[AvaContext],
    config: RunnableConfig,
) -> dict[str, Any] | None:
    if _summary_awaits_reply(state.messages):
        return None
    occupancy = _context_occupancy(state.messages)
    agent = runtime.context.require_agent()
    catalog = runtime.context.require_catalog()
    budget = resolve_context_budget(agent.brain.llm_model, agent.overrides, catalog=catalog)
    if occupancy <= budget.hard_compact_tokens:
        return None
    content_msgs = conversation_messages(state.messages)
    if not content_msgs:
        logger.info(
            "[{label}] {body}",
            label="auto-compact",
            event="auto_compact",
            body=f"skip: nothing to compress (no conversation messages), tokens≈{occupancy}",
        )
        return None
    llm = runtime.context.llm
    if llm is None:
        raise RuntimeError("auto_compact_for_llm requires ctx.llm")
    logger.info(
        "[{label}] {body}",
        label="auto-compact",
        event="auto_compact",
        body=f"start: tokens≈{occupancy}, compressing {len(content_msgs)} messages",
    )
    agent_id = agent_id_from_config(config)
    publisher = runtime.context.event_publisher
    compact_run_id = emit_compact_started(publisher, agent_id, mode=CompactionMode.AUTO)

    try:
        async with subscribe_interrupt(
            runtime.context.ops_pool,
            agent_id,
            incarnation=runtime.context.original_incarnation,
            work=runtime.context.native_work,
            resources=runtime.context.hosted_resources,
        ) as interrupted:
            summary = await interruptible_model(
                auto_compact_summary(
                    list(state.messages),
                    llm,
                    len(content_msgs),
                    runtime.context.require_agent(),
                    catalog=catalog,
                ),
                interrupted,
            )
    except ModelInterruptedError:
        emit_compact_finished(publisher, agent_id, compact_run_id, status=CompactionStatus.REPLACED)
        if publisher is not None:
            publisher.emit(Cancelled(agent_id=agent_id).model_dump_json())
        return {"halted": True, "goto": CLAIM}
    except BaseException:
        emit_compact_finished(publisher, agent_id, compact_run_id, status=CompactionStatus.FAILURE)
        raise

    logger.info(
        "[{label}] {body}",
        label="auto-compact",
        event="auto_compact",
        body=f"done: summary {len(summary)} chars",
    )

    emit_compaction_monitoring(
        state.messages,
        summary,
        agent_id=agent_id,
        compact_kind="auto",
        compact=state.compact,
    )
    pool = runtime.context.ops_pool

    if pool is not None:
        await record_audit_reported_async(
            pool,
            prepare_event_log(
                event_type="compact",
                agent_id=agent_id,
                source="system",
                payload={"compact_kind": "auto", "length": len(summary)},
            ),
        )

    if publisher is not None:
        publisher.emit(CompactDone(agent_id=agent_id).model_dump_json())

    dump_path = dump_history(state.messages, agent_id, runtime.context.require_agent().history_dump)
    summary_kwargs: dict[str, Any] = {
        "additional_kwargs": {
            "ava_msg_type": AvaMsgType.COMPACT_SUMMARY.value,
            "ava_created_at": datetime.now(UTC).isoformat(),
        },
    }
    if compact_run_id is not None:
        summary_kwargs["additional_kwargs"]["ava_compact_id"] = compact_run_id
    transition = build_compact_transition(
        summary,
        resume=CLAIM,
        extra_msgs=([history_dump_note(dump_path)] if dump_path is not None else None),
        summary_kwargs=summary_kwargs,
    )
    await stamp_compact_boundary(
        runtime.context.ops_pool,
        agent_id,
        state,
        closing_of(summary),
        read_agent=lambda field: runtime.context.require_agent().read("agent", field),
    )
    transition["compact"] = state.compact.next_segment()
    emit_compact_finished(publisher, agent_id, compact_run_id, status=CompactionStatus.SUCCESS)
    return transition


# ava:ref agent/graph/docs/context-window/replacement.ava.okf.md#clean-replacement
def build_compact_transition(
    summary: str,
    *,
    resume: NodeName,
    extra_msgs: list[AnyMessage] | None = None,
    summary_kwargs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    summary_msg_kwargs = summary_kwargs or {}
    tail_msgs: list[AnyMessage] = [
        HumanMessage(
            content=compose_summary_message(summary),
            **summary_msg_kwargs,
        ),
    ]
    if extra_msgs:
        tail_msgs.extend(extra_msgs)
    return {
        "messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES)],
        "context_reset": ContextReset(
            tail=tail_msgs,
            resume=resume,
        ),
        "goto": INIT_CONTEXT,
    }


COMPACT_REMINDER_NOTE = (
    "Your working context has reached the soft compaction threshold. "
    "Wind down this context now: finish only the immediate safe step, "
    "persist new durable information, and complete the preparation below. "
    "Then call ava.self.compact(summary) before starting further substantive work. "
    "Keep preparation brief; do not start a new investigation or wait for the "
    "hard limit, where automatic compaction cannot run file-writing tools. "
    "Write the summary as its docstring specifies. "
    "Read ava.help(ava.self.compact) if you need the contract."
)


# ava:ref agent/graph/docs/context-window/preparation.ava.okf.md#soft-preparation-delivery
def _reminder_content(
    agent: AgentSlices, *, catalog: ModelCatalog, extensions: ExtensionRegistry
) -> str:
    parts = [COMPACT_REMINDER_NOTE]
    if agent.read("agent", "workspace_in_system_prompt"):
        parts.append(
            f"Update your workspace's `{DOCUMENT_FILENAME}` shared with the user if it is stale: "
            "preserve current results, decisions, open questions and next steps; "
            "remove outdated status. Edit in place, without a separate notification."
        )
    for plugin, section in extensions.compact_reminder_sections():
        contribution = section(agent, catalog=catalog)
        if not isinstance(contribution, str):
            raise TypeError(f"{plugin}.{section.__name__} must return reminder text")
        if contribution.strip():
            parts.append(f"[{plugin}]\n{contribution.strip()}")
            activation.record(
                plugin,
                "compactReminderSections",
                section.__name__,
                model=agent.brain.llm_model,
                detail=f"chars={len(contribution)}",
            )
    return "\n\n".join(parts)


# ava:ref agent/graph/docs/context-window/preparation.ava.okf.md#soft-preparation-delivery
def compact_reminder_update(
    state: AgentState,
    agent: AgentSlices,
    *,
    catalog: ModelCatalog,
    extensions: ExtensionRegistry,
) -> dict[str, Any] | None:
    occupancy = _context_occupancy(state.messages)
    if (
        occupancy
        <= resolve_context_budget(
            agent.brain.llm_model, agent.overrides, catalog=catalog
        ).soft_compact_tokens
    ):
        return None
    if not conversation_messages(state.messages):
        return None
    compact: CompactState = state.compact
    shown: bool = compact.reminder_shown
    seen: int = compact.reminder_seen_version
    if compact.version > seen:
        shown = False
        seen = compact.version
    if shown:
        return None

    logger.info(
        "[{label}] {body}",
        label="compact-reminder",
        event="compact_reminder",
        body=f"injecting wind-down note, tokens≈{occupancy}",
    )
    return {
        "messages": [
            system_note_message(
                content=_reminder_content(agent, catalog=catalog, extensions=extensions),
                tag=NoteTag.COMPACT_REMINDER,
                created_at=datetime.now(UTC),
            )
        ],
        "compact": compact.model_copy(
            update={"reminder_shown": True, "reminder_seen_version": seen}
        ),
    }


class _CompactReminderHook(Hook):
    async def __call__(
        self,
        state: AgentState,
        runtime: Runtime[AvaContext],
        _config: RunnableConfig,
        /,
    ) -> dict[str, Any] | None:
        agent = runtime.context.require_agent()
        catalog = runtime.context.require_catalog()
        if _summary_awaits_reply(state.messages) or auto_compact_will_fire(
            state, agent, catalog=catalog
        ):
            return None
        return compact_reminder_update(
            state, agent, catalog=catalog, extensions=runtime.context.plugin_registry()
        )


compact_reminder = _CompactReminderHook()
