from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from langchain_core.messages import AnyMessage, BaseMessage, RemoveMessage
from langgraph.types import Command

from agent import state as _state
from agent.db import ClaimedInbound, finalize_claimed_inbounds
from agent.graph.claim._batch import defer_chats_to_pending
from agent.graph.claim._dispatch import BatchState
from agent.graph.claim._routing import ClaimGoto, Routing
from agent.graph.hooks.compact import (
    build_compact_transition,
    emit_compaction_monitoring,
    stamp_compact_boundary,
)
from agent.hooks.compact_anchor import closing_of
from agent.hooks.compact_events import emit_compact_finished
from agent.hooks.history_dump import dump_history, history_dump_note
from agent.nodes import BEFORE_LLM, CLAIM, END, INIT_CONTEXT
from base.agents.context import MeivaContext
from base.agents.messages.inbound import InboundKind
from base.agents.messages.kwargs import MeivaMsgType, NoteTag, read_meiva_kwargs
from base.events.live.projection import CompactDone, CompactionStatus


@dataclass(frozen=True)
class _Outcome:
    command: Command[ClaimGoto]
    publish_end_snapshot: bool = False


# meiva:ref agent/graph/docs/context-window/replacement.meiva.okf.md#clean-replacement
def _markers_only(messages: list[BaseMessage]) -> list[BaseMessage]:
    kept: list[BaseMessage] = []
    subject_kept = False
    for message in messages:
        kwargs = read_meiva_kwargs(message)
        keep = kwargs.get("meiva_msg_type") == MeivaMsgType.SYSTEM_NOTE.value
        if keep and kwargs.get("meiva_note_tag") == NoteTag.SECURITY.value:
            keep = subject_kept
        if keep:
            kept.append(message)
        subject_kept = keep
    return kept


# meiva:ref agent/graph/docs/context-window/replacement.meiva.okf.md#compact-failure-boundary
def _cancel_outcome(ctx: MeivaContext, agent_id: int, st: BatchState) -> _Outcome:
    if st.compact_payload is not None and st.compact_payload[2] is not None:
        emit_compact_finished(
            ctx.event_publisher, agent_id, st.compact_payload[2], status=CompactionStatus.REPLACED
        )
    if st.committed_chat_ids:
        return _Outcome(
            command=Command[ClaimGoto](
                update={"messages": st.new_msgs, "halted": False},
                goto=BEFORE_LLM,
            )
        )
    return _Outcome(
        command=Command[ClaimGoto](
            update={"messages": st.new_msgs, "halted": True},
            goto=CLAIM,
        )
    )


# meiva:ref agent/graph/docs/context-window/replacement.meiva.okf.md#clean-replacement
async def _compact_outcome(
    ctx: MeivaContext, state: _state.AgentState, agent_id: int, st: BatchState
) -> _Outcome:
    if st.compact_payload is None:
        raise RuntimeError("decide compact path requires a compact payload")
    summary_text, compact_kind, compact_run_id = st.compact_payload
    if ctx.event_publisher is None:
        raise RuntimeError("decide compact path requires ctx.event_publisher")
    emit_compaction_monitoring(
        state.messages,
        summary_text,
        agent_id=agent_id,
        compact_kind=compact_kind,
        compact=state.compact,
    )
    ctx.event_publisher.emit(CompactDone(agent_id=agent_id).model_dump_json())
    emit_compact_finished(
        ctx.event_publisher, agent_id, compact_run_id, status=CompactionStatus.SUCCESS
    )
    await stamp_compact_boundary(
        ctx.ops_pool,
        agent_id,
        state,
        closing=closing_of(summary_text),
        read_agent=lambda field: ctx.require_agent().read("agent", field),
    )
    if st.committed_chat_ids:
        await defer_chats_to_pending(
            ctx.ops_pool, agent_id, st.committed_chat_ids, incarnation=ctx.original_incarnation
        )
        st.new_msgs = _markers_only(st.new_msgs)
        st.committed_chat_ids = []
    await finalize_claimed_inbounds(ctx.ops_pool, agent_id, incarnation=ctx.original_incarnation)
    halted = st.restart_preserves_idle and not st.committed_chat_ids
    dump_path = dump_history(state.messages, agent_id, ctx.require_agent().history_dump)
    if dump_path is not None:
        st.new_msgs.append(history_dump_note(dump_path))
    extra_msgs = [cast(AnyMessage, m) for m in st.new_msgs if not isinstance(m, RemoveMessage)]
    summary_kwargs: dict[str, str] = {
        "meiva_msg_type": compact_kind,
        "meiva_created_at": datetime.now(UTC).isoformat(),
    }
    if compact_run_id is not None:
        summary_kwargs["meiva_compact_id"] = compact_run_id
    transition = build_compact_transition(
        summary_text,
        resume=st.next_goto,
        extra_msgs=extra_msgs,
        summary_kwargs={"additional_kwargs": summary_kwargs},
    )
    return _Outcome(
        command=Command[ClaimGoto](
            update={
                "messages": transition["messages"],
                "context_reset": transition["context_reset"],
                "halted": halted,
                "update_initiated": st.update_initiated,
                "compact": state.compact.next_segment(),
            },
            goto=INIT_CONTEXT,
        )
    )


def _fallthrough_outcome(st: BatchState) -> _Outcome:

    publish_snapshot = st.next_goto == END and bool(st.new_msgs)
    halted = st.restart_preserves_idle and not st.committed_chat_ids
    goto = BEFORE_LLM if st.committed_chat_ids and st.next_goto == CLAIM else st.next_goto
    return _Outcome(
        command=Command[ClaimGoto](
            update={
                "messages": st.new_msgs,
                "halted": halted,
                "update_initiated": st.update_initiated,
            },
            goto=goto,
        ),
        publish_end_snapshot=publish_snapshot,
    )


def _veto_reentry(routing: Routing, st: BatchState) -> bool:
    return (
        routing.terminate_vetoed_by_pending
        and not st.committed_chat_ids
        and st.compact_payload is None
    )


def _idle_restart_gate(
    state: _state.AgentState, batch: list[ClaimedInbound], st: BatchState
) -> bool:
    return (
        state.halted
        and not st.update_initiated
        and all(it.kind == InboundKind.RESTART_COMPLETED for it in batch)
    )


async def decide(
    ctx: MeivaContext,
    state: _state.AgentState,
    agent_id: int,
    batch: list[ClaimedInbound],
    st: BatchState,
    routing: Routing,
) -> _Outcome:
    if st.cancelled and routing.cancelled_applies:
        return _cancel_outcome(ctx, agent_id, st)

    if _veto_reentry(routing, st):
        return _Outcome(
            command=Command[ClaimGoto](
                update={"messages": st.new_msgs, "halted": False},
                goto=CLAIM,
            )
        )

    if _idle_restart_gate(state, batch, st):
        return _Outcome(
            command=Command[ClaimGoto](
                update={"messages": st.new_msgs, "halted": True},
                goto=CLAIM,
            )
        )

    if st.compact_payload is not None:
        return await _compact_outcome(ctx, state, agent_id, st)

    return _fallthrough_outcome(st)
