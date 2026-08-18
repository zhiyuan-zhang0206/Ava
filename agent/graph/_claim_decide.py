"""Post-dispatch decision for the claim node: short-circuit rules → one Command.

Extracted from agent/graph/_claim.py (Task #1006 split). Every return path
flows through decide() — the original's eight return points collapse to one;
the ``halted`` formula appears exactly once. Chain: cancel path → veto
re-entry → idle-restart gate → compact path → normal fallthrough (with the
END snapshot flag).

State typing follows the agent/graph/_exec.py module-docstring pattern
(``_state.AgentState`` + deferred annotations) — see _claim.py docstring.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from langgraph.types import Command

from agent import state as _state
from agent.db import ClaimedInbound, finalize_claimed_inbounds
from agent.graph._claim_batch import _defer_chats_to_pending
from agent.graph._claim_dispatch import _BatchState
from agent.graph._claim_routing import ClaimGoto, _Routing
from agent.graph._context import AvaContext
from agent.graph._nodes import BEFORE_LLM, CLAIM, END, INIT_CONTEXT
from agent.history_dump import dump_history, history_dump_note
from agent.hooks.compact import build_compact_transition, trim_checkpoints_after_compact
from shared.inbound import InboundKind
from shared.live_events import CompactDone
from shared.message_kwargs import AvaMsgType


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
    # ── Cancel path ──
    # Pause, but only when neither an exit nor a revive shares the batch.
    if st.cancelled and routing.cancelled_applies:
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

    # ── Veto re-entry ──
    if (
        routing.terminate_vetoed_by_pending
        and not st.committed_chat_ids
        and st.compact_payload is None
    ):
        return _Outcome(
            command=Command[ClaimGoto](
                update={"messages": st.new_msgs, "halted": False},
                goto=CLAIM,
            )
        )

    # ── Idle-restart gate ──
    if (
        state.halted
        and not st.update_initiated
        and all(it.kind == InboundKind.RESTART_COMPLETED for it in batch)
    ):
        return _Outcome(
            command=Command[ClaimGoto](
                update={"messages": st.new_msgs, "halted": True},
                goto=CLAIM,
            )
        )

    # ── Compact path ──
    if st.compact_payload is not None:
        summary_text, compact_kind = st.compact_payload
        assert ctx.event_publisher is not None, "decide compact path requires ctx.event_publisher"  # noqa: S101
        ctx.event_publisher.emit(CompactDone(agent_id=agent_id).model_dump_json())
        await trim_checkpoints_after_compact(ctx.ops_pool, agent_id)
        # Defer any chats co-batched with the compact: they arrived while the
        # turn was in flight and were never part of the summarized history, so
        # they must survive — but as pending inbounds delivered in the fresh
        # context, not as raw messages parked after the summary. The compact
        # itself is a clean wipe: only the summary and framework lifecycle
        # markers (resurrect / fork) ride the tail — never raw conversation.
        if st.committed_chat_ids:
            await _defer_chats_to_pending(ctx.ops_pool, agent_id, st.committed_chat_ids)
            st.new_msgs = [
                m
                for m in st.new_msgs
                if m.additional_kwargs.get("ava_msg_type") == AvaMsgType.SYSTEM_NOTE.value  # pyright: ignore[reportUnknownMemberType]
            ]
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
        # Opt-in forensics: snapshot the full pre-compact conversation before
        # the wipe. The note rides the fresh context tail (after the summary),
        # never the pre-compact messages channel — a note between an AIMessage
        # and its ToolMessage is rejected by the DeepSeek anthropic endpoint,
        # and this whole window is about to be REMOVE_ALL'd anyway.
        dump_path = dump_history(state.messages, agent_id)
        if dump_path is not None:
            # The note is a system note like the lifecycle markers already in
            # st.new_msgs — it survives the chat-deferral filter above (which
            # keeps only SYSTEM_NOTE-typed messages) and rides the fresh tail.
            st.new_msgs.append(history_dump_note(dump_path))
        transition = build_compact_transition(
            summary_text,
            resume=st.next_goto,
            extra_msgs=list(st.new_msgs),  # pyright: ignore[reportUnknownArgumentType]
            summary_kwargs={
                "additional_kwargs": {
                    "ava_msg_type": compact_kind,
                    "ava_created_at": datetime.now(UTC).isoformat(),
                },
            },
        )
        return _Outcome(
            command=Command[ClaimGoto](
                update={
                    "messages": transition["messages"],
                    "context_reset": transition["context_reset"],
                    "halted": halted,
                    "update_initiated": st.update_initiated,
                    "compact": state.compact.model_copy(
                        update={"version": state.compact.version + 1}
                    ),
                },
                goto=INIT_CONTEXT,
            )
        )

    # ── Normal fallthrough ──
    # END snapshot needed when claim routes to END with new markers appended.
    publish_snapshot = st.next_goto == END and bool(st.new_msgs)
    halted = st.restart_preserves_idle and not st.committed_chat_ids
    return _Outcome(
        command=Command[ClaimGoto](
            update={
                "messages": st.new_msgs,
                "halted": halted,
                "update_initiated": st.update_initiated,
            },
            goto=st.next_goto,
        ),
        publish_end_snapshot=publish_snapshot,
    )
