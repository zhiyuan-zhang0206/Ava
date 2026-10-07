"""Post-dispatch decision for the claim node: short-circuit rules → one Command.

Extracted from agent/graph/claim/node.py (Task #1006 split). Every return path
flows through decide() — the original's eight return points collapse to one;
the ``halted`` formula appears exactly once. Chain: cancel path → veto
re-entry → idle-restart gate → compact path → normal fallthrough (with the
END snapshot flag).

State typing follows `agent/graph/exec/node.py` (``_state.AgentState``, static only).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from langchain_core.messages import AnyMessage, BaseMessage, RemoveMessage
from langgraph.types import Command

from agent import state as _state
from agent.db import ClaimedInbound, finalize_claimed_inbounds
from agent.graph.claim._batch import _defer_chats_to_pending
from agent.graph.claim._dispatch import _BatchState
from agent.graph.claim._routing import ClaimGoto, _Routing
from agent.hooks.compact import (
    CompactionFailedError,
    build_compact_transition,
    conversation_messages,
    emergency_compact_summary,
    emit_compaction_monitoring,
    stamp_compact_boundary,
)
from agent.hooks.compact_anchor import closing_of
from agent.hooks.compact_events import emit_compact_finished, emit_compact_started
from agent.hooks.history_dump import dump_history, history_dump_note
from agent.nodes import BEFORE_LLM, CLAIM, END, INIT_CONTEXT
from agent.state_channels import CIRCUIT_REASON_CONTEXT_OVERFLOW
from base.agents.context import AvaContext
from base.agents.messages.inbound import InboundKind
from base.agents.messages.kwargs import AvaMsgType, NoteTag, read_ava_kwargs
from base.events.live.projection import CompactDone, CompactionMode, CompactionStatus
from base.log import logger


@dataclass(frozen=True)
class _Outcome:
    """The final result of one claim pass.

    ``command`` is the Command to return from the node.
    ``publish_end_snapshot`` tells the caller to emit a TimelineSnapshot
    before returning (needed when claim routes to END with new markers that
    the enter-time snapshot missed).
    """

    command: Command[ClaimGoto]
    publish_end_snapshot: bool = False


def _markers_only(messages: list[BaseMessage]) -> list[BaseMessage]:
    """The framework markers of a batch whose chats are deferred: system notes
    survive, chat messages do not.

    A SECURITY note rides right behind the message it flags, so one behind a
    dropped chat goes with it — the deferred chat is claimed and scanned again
    in the fresh context, which raises its note then.
    """
    kept: list[BaseMessage] = []
    subject_kept = False
    for message in messages:
        kwargs = read_ava_kwargs(message)
        keep = kwargs.get("ava_msg_type") == AvaMsgType.SYSTEM_NOTE.value
        if keep and kwargs.get("ava_note_tag") == NoteTag.SECURITY.value:
            keep = subject_kept
        if keep:
            kept.append(message)
        subject_kept = keep
    return kept


def _cancel_outcome(ctx: AvaContext, agent_id: int, st: _BatchState) -> _Outcome:
    """The cancel path: pause (or revive when a chat is committed in the same batch)."""
    # Both branches below drop any pending compact payload instead of
    # applying it — a run that generated a summary this pass can never
    # land once the cancel wins the batch, so its live block closes as
    # `replaced` (every started run reaches exactly one terminal state).
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


async def _force_circuit_compact(
    ctx: AvaContext, state: _state.AgentState, agent_id: int, st: _BatchState
) -> None:
    """Overflow self-rescue: compact on this wake instead of the doomed LLM call."""
    # The heartbeat circuit breaker is open with reason=context_overflow: the
    # provider permanently rejected the last LLM call because the context
    # exceeds the window. Every wake routes here instead of into the doomed
    # call — a real compaction first, then the no-LLM minimal fallback when
    # the compaction request itself is rejected (emergency_compact_summary).
    # The result flows into the compact path below (transition, checkpoint
    # trim, chat deferral, version bump) exactly like a /compact.
    assert ctx.llm is not None, "circuit-breaker compact requires ctx.llm"  # noqa: S101
    logger.warning(
        "circuit breaker open (context_overflow) — forcing compaction on "
        "this wake instead of the doomed LLM call",
        event="circuit_breaker_compact",
        agent_id=agent_id,
    )
    compact_run_id = emit_compact_started(ctx.event_publisher, agent_id, mode=CompactionMode.AUTO)
    try:
        summary = await emergency_compact_summary(state.messages, ctx.llm, ctx.require_agent())
    except CompactionFailedError:
        # Transient failures exhausted — the fallback rescue did not
        # happen either; close the live block before the turn aborts.
        emit_compact_finished(
            ctx.event_publisher, agent_id, compact_run_id, status=CompactionStatus.FAILURE
        )
        raise
    st.compact_payload = (summary, AvaMsgType.COMPACT_REQUEST.value, compact_run_id)


async def _compact_outcome(
    ctx: AvaContext, state: _state.AgentState, agent_id: int, st: _BatchState
) -> _Outcome:
    """The compact path: summary transition, chat deferral, checkpoint trim, version bump."""
    assert st.compact_payload is not None  # noqa: S101
    summary_text, compact_kind, compact_run_id = st.compact_payload
    assert ctx.event_publisher is not None, "decide compact path requires ctx.event_publisher"  # noqa: S101
    emit_compaction_monitoring(
        state.messages,
        summary_text,
        agent_id=agent_id,
        compact_kind=compact_kind,
    )
    ctx.event_publisher.emit(CompactDone(agent_id=agent_id).model_dump_json())
    # Terminal signal for the run's live block; CompactDone above keeps its
    # own meaning (messages modified in place — UI re-fetch).
    emit_compact_finished(
        ctx.event_publisher, agent_id, compact_run_id, status=CompactionStatus.SUCCESS
    )
    await stamp_compact_boundary(ctx.ops_pool, agent_id, state, closing=closing_of(summary_text))
    # Defer any chats co-batched with the compact: they arrived while the
    # turn was in flight and were never part of the summarized history, so
    # they must survive — but as pending inbounds delivered in the fresh
    # context, not as raw messages parked after the summary. The compact
    # itself is a clean wipe: only the summary and framework lifecycle
    # markers (resurrect / fork) ride the tail — never raw conversation.
    if st.committed_chat_ids:
        await _defer_chats_to_pending(ctx.ops_pool, agent_id, st.committed_chat_ids)
        st.new_msgs = _markers_only(st.new_msgs)
        st.committed_chat_ids = []
    # Finalize every remaining claimed inbound before the wipe: their
    # HumanMessages live in state.messages (about to be REMOVE_ALL'd) and
    # carry the ava_inbound_id startup reconcile matches on. Without this,
    # the next restart sees every claimed row missing from the checkpoint,
    # resets them to 'pending', and re-delivers already-answered messages
    # — a run of consecutive user messages with the compacted replies
    # gone (Task #823).
    await finalize_claimed_inbounds(ctx.ops_pool, agent_id)
    halted = st.restart_preserves_idle and not st.committed_chat_ids
    # Pre-compact history dump: snapshot the full conversation before the
    # wipe. The note rides the fresh context tail (after the summary),
    # never the pre-compact messages channel — a note between an AIMessage
    # and its ToolMessage is rejected by the DeepSeek anthropic endpoint,
    # and this whole window is about to be REMOVE_ALL'd anyway.
    dump_path = dump_history(state.messages, agent_id, ctx.require_agent().history_dump)
    if dump_path is not None:
        # The note is a system note like the lifecycle markers already in
        # st.new_msgs — it survives the chat-deferral filter above (which
        # keeps only SYSTEM_NOTE-typed messages) and rides the fresh tail.
        st.new_msgs.append(history_dump_note(dump_path))
    # The fork strip entries are channel operations (RemoveMessage),
    # not content the compact summary may carry — and build_compact_transition
    # types extra_msgs as AnyMessage, which excludes them.
    extra_msgs = [cast(AnyMessage, m) for m in st.new_msgs if not isinstance(m, RemoveMessage)]
    summary_kwargs: dict[str, str] = {
        "ava_msg_type": compact_kind,
        "ava_created_at": datetime.now(UTC).isoformat(),
    }
    # The durable anchor tying this summary to its live run — the auto
    # path's summary carries the same key (identical message contract).
    if compact_run_id is not None:
        summary_kwargs["ava_compact_id"] = compact_run_id
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


def _fallthrough_outcome(st: _BatchState) -> _Outcome:
    """Normal fallthrough: the batch's own update, with the END snapshot flag."""

    # END snapshot needed when claim routes to END with new markers appended.
    publish_snapshot = st.next_goto == END and bool(st.new_msgs)
    halted = st.restart_preserves_idle and not st.committed_chat_ids
    # A non-overflow open-breaker heartbeat parks at CLAIM. Its co-batched chat
    # is real work already committed to this update, so it must reach the LLM;
    # only CLAIM has this heartbeat-park meaning in normal fallthrough.
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


def _veto_reentry(routing: _Routing, st: _BatchState) -> bool:
    return (
        routing.terminate_vetoed_by_pending
        and not st.committed_chat_ids
        and st.compact_payload is None
    )


def _idle_restart_gate(
    state: _state.AgentState, batch: list[ClaimedInbound], st: _BatchState
) -> bool:
    return (
        state.halted
        and not st.update_initiated
        and all(it.kind == InboundKind.RESTART_COMPLETED for it in batch)
    )


def _circuit_overflow(state: _state.AgentState, st: _BatchState) -> bool:
    """The heartbeat breaker is open with context_overflow and nothing else claims the wake."""
    return (
        st.compact_payload is None
        and st.next_goto == BEFORE_LLM
        and not st.cancelled
        and state.circuit.open
        and state.circuit.reason == CIRCUIT_REASON_CONTEXT_OVERFLOW
        and bool(conversation_messages(state.messages))
    )


async def decide(
    ctx: AvaContext,
    state: _state.AgentState,
    agent_id: int,
    batch: list[ClaimedInbound],
    st: _BatchState,
    routing: _Routing,
) -> _Outcome:
    """Post-dispatch decision: chain of short-circuit rules → single Command.

    Every return path flows through this one function — the original's eight
    return points collapse to one.  The ``halted`` formula appears exactly once.
    """
    if st.cancelled and routing.cancelled_applies:
        return _cancel_outcome(ctx, agent_id, st)

    # ── Veto re-entry ──
    if _veto_reentry(routing, st):
        return _Outcome(
            command=Command[ClaimGoto](
                update={"messages": st.new_msgs, "halted": False},
                goto=CLAIM,
            )
        )

    # ── Idle-restart gate ──
    if _idle_restart_gate(state, batch, st):
        return _Outcome(
            command=Command[ClaimGoto](
                update={"messages": st.new_msgs, "halted": True},
                goto=CLAIM,
            )
        )

    if _circuit_overflow(state, st):
        await _force_circuit_compact(ctx, state, agent_id, st)

    if st.compact_payload is not None:
        return await _compact_outcome(ctx, state, agent_id, st)

    return _fallthrough_outcome(st)
