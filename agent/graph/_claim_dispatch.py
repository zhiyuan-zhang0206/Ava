"""Per-kind inbound dispatch for the claim node.

Chat appends an envelope; compaction replaces history with its summary.
Terminate and restart append acceptance markers and end the invocation so the
host can flush the checkpoint before applying the command. Resurrect and fork
append identity markers. Historical restart_completed messages remain readable.
Routing guards select lifecycle and compaction winners; unknown kinds fail fast.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

from langchain_core.messages import BaseMessage, RemoveMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from agent import state as _state
from agent.db import ClaimedInbound
from agent.graph._chat_inbound import build_chat_inbound
from agent.graph._context import AvaContext
from agent.graph._context_notes import fork_notes
from agent.graph._nodes import BEFORE_LLM, CLAIM, END
from agent.hooks.compact import (
    COMPACT_MAX_ATTEMPTS,
    CompactionFailedError,
    conversation_messages,
    generate_summary,
)
from agent.messages import NoteTag, system_note_message
from agent.state_channels import CIRCUIT_REASON_CONTEXT_OVERFLOW
from ava.security import scan_content
from shared.config import now_timestamp, settings
from shared.inbound import InboundKind
from shared.live_events import Cancelled
from shared.log import logger
from shared.message_kwargs import AvaMsgType, read_ava_kwargs

from ._claim_routing import _ROUTING_KINDS, ClaimGoto, _Routing


@dataclass
class _BatchState:
    """Mutable accumulator carried through one dispatch pass.

    Initialised with the inheritable channel values from AgentState; mutated
    in-place by the per-kind handlers during dispatch_batch().
    """

    new_msgs: list[BaseMessage] = field(default_factory=list)
    """Messages appended this pass — HumanMessages from the handlers plus,
    for a fork, the `RemoveMessage` strip entries (issue #1320)."""
    next_goto: ClaimGoto = BEFORE_LLM
    compact_payload: tuple[str, str] | None = None  # (summary_text, compact_kind)
    cancelled: bool = False
    restart_preserves_idle: bool = False
    restart_requested: bool = False
    update_initiated: bool = False
    active_task_id: int | None = None
    task_ids: set[int] = field(default_factory=set)
    committed_chat_ids: list[int] = field(default_factory=list)


def _by_who(source: str) -> str:
    """Render "by X" in lifecycle marker text.

    'self' (ava.self.terminate / restart called by agent itself) → "yourself"
    makes the marker read naturally ("You are terminated by yourself" expresses
    "self-killed" semantics more accurately than "by self").
    Other sources ('user' / 'agent:N' / 'system') concatenate the
    raw value — the agent immediately sees the trigger's identity.
    """
    if source == "self":
        return "yourself"
    return source


def _ts_prefix() -> str:
    """Leading `[ts] ` for lifecycle markers, or `` when agent-facing message
    timestamps are off (`settings.general.message_timestamps`)."""
    return f"{now_timestamp()} " if settings.general.message_timestamps else ""


# Overlay keys that may carry credentials — their values are never rendered
# into a restart marker, which lands in the checkpoint, the frontend timeline
# and the LLM context (2026-08-08 audit, P1-3). Match on the normalized name so
# `api_key` / `provider_token` / `cluster_secret` are all caught; over-matching
# only redacts a non-secret from the marker text, which costs nothing.
_SENSITIVE_OVERLAY_KEY_FRAGMENTS = ("api_key", "apikey", "token", "secret", "password")


def _is_sensitive_overlay_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(fragment in normalized for fragment in _SENSITIVE_OVERLAY_KEY_FRAGMENTS)


def _render_restart_completed_marker(source: str, payload: dict[str, object] | None = None) -> str:
    """Lifecycle marker text for restart_completed inbound.

    Update sources (`system:update`) get distinct wording so the agent can tell
    an update rollout from an ordinary restart:
    - `system:update` → "You have been updated and restarted" (no actor suffix —
      a system-driven cluster rollout has no single human actor). This is the
      marker each agent wakes to on the new code after a rollout.
    - Any other source → "You have been restarted by {_by_who(source)}"

    Payload contains ``config_overlay`` (`ava.self.restart(config_overlay=...)` path) →
    append ``with config {k=v, ...}`` at the end so the agent immediately sees the
    per-agent overlay fields actually in effect for this process. Empty / missing
    overlay does not append the suffix. Overlay values for credential-like keys
    (``*_api_key`` / ``*_token`` / ``*_secret`` / ``*_password``) render as
    ``<redacted>`` — the marker is a new plaintext copy of the overlay that lands
    in the checkpoint / timeline / LLM context.
    """
    ts = _ts_prefix()
    if source == "system:update":
        base = f"{ts}You have been updated and restarted"
    else:
        base = f"{ts}You have been restarted by {_by_who(source)}"

    if payload:
        overlay = payload.get("config_overlay")
        if isinstance(overlay, dict) and overlay:
            overlay = cast(dict[str, Any], overlay)
            diff = ", ".join(
                f"{k}=<redacted>" if _is_sensitive_overlay_key(k) else f"{k}={v!r}"
                for k, v in sorted(overlay.items())
            )
            base = f"{base} with config {{{diff}}}"
    return base


async def _handle_chat(
    item: ClaimedInbound,
    st: _BatchState,
) -> None:
    """CHAT inbound: wrap as HumanMessage, append to state, mark committed."""
    st.new_msgs.append(build_chat_inbound(item))
    st.active_task_id = None
    st.committed_chat_ids.append(item.id)


def _system_note_tag(payload: dict[str, object] | None) -> NoteTag:
    """The NoteTag for a system_note inbound, carried in its payload.

    `note_tag` is the one payload key this kind reads; missing, non-string,
    or unknown fails loud rather than silently defaulting — a wrong tag is a
    writer bug and renders as the wrong timeline chip.
    """
    if not payload:
        raise ValueError("system_note inbound requires a payload with 'note_tag'")
    raw = payload.get("note_tag")
    if not isinstance(raw, str):
        raise TypeError(f"system_note inbound 'note_tag' must be a NoteTag value, got {raw!r}")
    try:
        return NoteTag(raw)
    except ValueError as exc:
        raise ValueError(f"system_note inbound 'note_tag' {raw!r} is not a NoteTag value") from exc


def _task_id_from_system_note(payload: dict[str, object] | None, tag: NoteTag) -> int | None:
    """Return the explicit task attribution from a task note, if present."""
    if tag is not NoteTag.TASK or not payload or "task_id" not in payload:
        return None
    task_id = payload["task_id"]
    if not isinstance(task_id, int) or isinstance(task_id, bool) or task_id <= 0:
        raise ValueError(
            f"system_note inbound 'task_id' must be a positive integer, got {task_id!r}"
        )
    return task_id


async def _handle_system_note(
    item: ClaimedInbound,
    st: _BatchState,
) -> None:
    """SYSTEM_NOTE inbound: render as a system note (system_marker), not chat.

    The note's content is peer-authored (the task title / description / change
    summary written by another agent), so it passes through the same
    injection scan as inbound chat before entering the conversation.
    """
    content = item.content
    if settings.agent.security_scan_enabled:
        content = scan_content(content, source=f"inbound.system_note:{item.source}")
    task_id = _task_id_from_system_note(item.payload, _system_note_tag(item.payload))
    st.active_task_id = task_id
    if task_id is not None:
        st.task_ids.add(task_id)
    st.new_msgs.append(
        system_note_message(
            content=f"{_ts_prefix()}{content}",
            tag=_system_note_tag(item.payload),
            task_id=task_id,
            created_at=item.created_at,
        )
    )


async def _handle_compact_summary(
    item: ClaimedInbound,
    st: _BatchState,
) -> None:
    """COMPACT_SUMMARY: agent-authored summary — used directly, no LLM run."""
    st.compact_payload = (item.content, AvaMsgType.COMPACT_SUMMARY.value)


async def _handle_compact_request(
    ctx: AvaContext,
    state: _state.AgentState,
    _item: ClaimedInbound,
    st: _BatchState,
) -> None:
    """COMPACT_REQUEST: run Compaction LLM to generate summary.

    Dropped under an exit winner (the guard is in dispatch_batch via
    _Routing.is_winner). An empty conversation is a normal state for a
    user-issued /compact (fresh agent), not a fault — consumed as a logged
    no-op.
    """
    assert ctx.llm is not None, "_handle_compact_request requires ctx.llm"  # noqa: S101
    if not conversation_messages(state.messages):
        logger.info(
            "[{label}] {body}",
            label="compact-request",
            body="request consumed as no-op: no conversation to compress",
            event="compact_request",
        )
        return
    # The compaction LLM call can fail (provider error, empty output). Retry
    # like the auto path (COMPACT_MAX_ATTEMPTS); when every attempt fails,
    # raise CompactionFailedError — the runloop turns that into a turn-abort
    # (agent stays alive, idles for the next inbound) instead of an unhandled
    # exception killing the process into a non-resurrectable 'exit'. The
    # request row is already consumed ('done'), so the user re-issues
    # /compact; the Error event tells them why.
    last_error: Exception | None = None
    summary = ""
    for attempt in range(1, COMPACT_MAX_ATTEMPTS + 1):
        try:
            summary = await generate_summary(state.messages, ctx.llm)
            break
        except Exception as e:
            last_error = e
            logger.warning(
                "[{label}] {body}",
                label="compact-request",
                event="compact_request",
                body=f"attempt {attempt}/{COMPACT_MAX_ATTEMPTS}: {e}; retrying",
            )
    else:
        raise CompactionFailedError(
            f"Compaction LLM produced no usable summary across {COMPACT_MAX_ATTEMPTS}"
            f" attempts (last: {last_error!r}) — compact_request not applied"
        ) from last_error
    logger.info(
        "[{label}] {body}",
        label="compact-request",
        body=f"request summary {len(summary)} chars",
        event="compact_request",
    )
    st.compact_payload = (summary, AvaMsgType.COMPACT_REQUEST.value)


async def _handle_cancel(
    ctx: AvaContext,
    agent_id: int,
    st: _BatchState,
) -> None:
    """CANCEL: pause agent — stop in-flight work, idle. No lifecycle marker."""
    assert ctx.event_publisher is not None, "_handle_cancel requires ctx.event_publisher"  # noqa: S101
    ctx.event_publisher.emit(Cancelled(agent_id=agent_id).model_dump_json())
    st.cancelled = True


async def _handle_heartbeat(
    ctx: AvaContext,
    agent_id: int,
    item: ClaimedInbound,
    st: _BatchState,
    state: _state.AgentState,
) -> None:
    """HEARTBEAT: check-in delivered as a system note, plus page liveness.

    The page-server daemon supervises serve() pages inside persistent agent
    shell sessions, which are outside rollout service teardown. Probing on
    each heartbeat (default 5 min) remains the catch-all for server death:
    dead serve_dir pages are re-served and dead no-dir pages are closed.
    The periodic page_reconcile_loop (agent/startup.py) covers busy agents
    whose heartbeats never arrive; this pass keeps the idle-agent cadence.
    Best-effort; reconcile never raises.

    With the heartbeat circuit breaker open (a permanent provider rejection
    aborted the last turn) the check-in is consumed WITHOUT its note and
    without routing to the LLM — the note would only re-fire the doomed call
    and grow the context (the 3962 incident: 80 heartbeat cycles against a
    permanent context-overflow 400). Page liveness still reconciles. The
    overflow reason routes to BEFORE_LLM anyway so decide()'s forced-compact
    arm runs; every other reason parks at CLAIM (the agent stays idling until
    a real wake — the breaker closes on the first successful LLM call).
    """
    assert ctx.ops_pool is not None, "_handle_heartbeat requires ctx.ops_pool"  # noqa: S101
    from agent.startup import reconcile_open_pages

    await reconcile_open_pages(ctx.ops_pool, agent_id, event_publisher=ctx.event_publisher)

    circuit = state.circuit
    if circuit.open:
        logger.warning(
            "heartbeat consumed while circuit breaker open (reason={reason}) — "
            "check-in note skipped, no LLM call",
            event="heartbeat_circuit_open",
            agent_id=agent_id,
            reason=circuit.reason,
        )
        if circuit.reason != CIRCUIT_REASON_CONTEXT_OVERFLOW:
            st.next_goto = CLAIM
        return
    st.new_msgs.append(
        system_note_message(
            content=f"{_ts_prefix()}{item.content}",
            tag=NoteTag.HEARTBEAT,
            created_at=datetime.now(UTC),
        )
    )


async def _handle_terminate(
    _ctx: AvaContext,
    item: ClaimedInbound,
    st: _BatchState,
) -> None:
    """TERMINATE: append lifecycle marker, route to END.

    Only called when this terminate IS the routing winner (guard in
    dispatch_batch). Vetoed terminates never reach here.
    """
    st.new_msgs.append(
        system_note_message(
            content=f"{_ts_prefix()}Termination was accepted from {_by_who(item.source)}",
            tag=NoteTag.LIFECYCLE_TERMINATE,
            created_at=datetime.now(UTC),
        )
    )
    st.next_goto = END


async def _handle_restart(
    _ctx: AvaContext,
    _agent_id: int,
    item: ClaimedInbound,
    st: _BatchState,
) -> None:
    """Render restart acceptance and end this invocation. The host applies the durable command after the graph returns and the checkpoint flushes."""
    st.new_msgs.append(
        system_note_message(
            content=f"{_ts_prefix()}Restart was accepted from {_by_who(item.source)}",
            tag=NoteTag.LIFECYCLE_RESTART,
            created_at=datetime.now(UTC),
        )
    )
    st.next_goto = END
    st.restart_requested = True


async def _handle_restart_stateful(
    state: _state.AgentState,
    item: ClaimedInbound,
    st: _BatchState,
) -> None:
    """Set restart_preserves_idle and update_initiated from state context.

    Separated from _handle_restart because these decisions depend on
    *state* (AgentState), which the dispatch loop already has.
    """
    st.restart_preserves_idle = (state.halted or not state.messages) and item.source not in (
        "self",
    )
    if item.source == "self":
        st.update_initiated = True
    if st.update_initiated:
        st.restart_preserves_idle = False


async def _handle_restart_completed(
    item: ClaimedInbound,
    st: _BatchState,
) -> None:
    """RESTART_COMPLETED: append lifecycle marker (correct tense).

    Never competes for routing — the marker is appended regardless of who
    won.  Tracks update_initiated for the idle-restart gate.
    """
    st.new_msgs.append(
        system_note_message(
            content=_render_restart_completed_marker(item.source, item.payload),
            tag=NoteTag.LIFECYCLE_RESTART,
            created_at=datetime.now(UTC),
        )
    )
    if item.source == "system:update" and st.update_initiated:
        st.update_initiated = False


async def _handle_resurrect(
    item: ClaimedInbound,
    st: _BatchState,
) -> None:
    """RESURRECT: append lifecycle marker, wake to BEFORE_LLM.

    Only called when no exit won (guard in dispatch_batch). When an exit
    beats the resurrect it is a consumed no-op — the agent is exiting,
    a 'resurrected' marker right before death would be noise.
    """
    st.new_msgs.append(
        system_note_message(
            content=f"{_ts_prefix()}You have been resurrected by {_by_who(item.source)}",
            tag=NoteTag.LIFECYCLE_RESURRECT,
            created_at=datetime.now(UTC),
        )
    )


# Notes the inherited history renders wrong for the new agent: each names the
# SOURCE (its id, its personal memory store, its preloaded-skill set). The fork
# drops the inherited copies in its full-wipe head rebuild and grafts the new
# agent's own — exactly one of each, owned by the agent reading it (issue
# #1320). The cluster memory index is deliberately NOT here: it is
# cluster-wide, so the inherited copy is the same content a graft would add
# (grafting it duplicated the index — the timezone note's reasoning applies to
# it too).
_STRIP_ON_FORK_TAGS: frozenset[NoteTag] = frozenset(
    {NoteTag.AGENT_ID, NoteTag.AGENT_MEMORY, NoteTag.PRELOADED_SKILLS}
)


def _is_source_identity_note(m: BaseMessage) -> bool:
    """Whether `m` is a standing note the fork must drop from the rebuild — a
    system note whose tag names the SOURCE agent."""
    kw = read_ava_kwargs(m)
    return (
        kw.get("ava_msg_type") == AvaMsgType.SYSTEM_NOTE.value
        and kw.get("ava_note_tag") in _STRIP_ON_FORK_TAGS
    )


def _fork_rebuild_prefix(state: _state.AgentState) -> list[BaseMessage]:
    """The full-wipe rebuild the fork prepends to its claim update: the
    inherited history re-listed UNCHANGED except that the source-identity
    notes are dropped.

    Mid-history `RemoveMessage(id=...)` deletion is forbidden by the
    append-only ruling (task #1256, `agent/messages_guard.py`); the full wipe
    is the one sanctioned deletion shape, and the guard's rebuild check only
    demands that survivors keep content + relative order — dropping the
    source notes and splicing the grafted own-copies afterwards satisfies it.
    A fork is a brand-new agent whose model has seen nothing yet, so the head
    rewrite breaks no "model-visible means logged" invariant.
    """
    return [
        RemoveMessage(id=REMOVE_ALL_MESSAGES),
        *[m for m in state.messages if not _is_source_identity_note(m)],
    ]


async def _handle_fork(
    agent_id: int,
    item: ClaimedInbound,
    st: _BatchState,
    state: _state.AgentState,
) -> None:
    """FORK: rebuild the head (drop source-identity notes), append marker, graft own notes.

    Fork copied the source agent's full history; without a marker the model
    keeps the source's identity.  State the new identity explicitly. Notes the
    inherited history renders wrong for the new agent are dropped from the
    rebuild and re-grafted with the new agent's content via `fork_notes`.

    The rebuild is prepended (not appended) to `st.new_msgs` so any chat /
    marker the same batch dispatched stays a tail append AFTER the rebuild —
    a wipe must never swallow a message another handler already appended.
    """
    st.new_msgs[0:0] = _fork_rebuild_prefix(state)
    st.new_msgs.append(
        system_note_message(
            content=(
                f"{_ts_prefix()}You have been forked from {item.source}. "
                f"You are now a new, independent agent with id {agent_id} — the "
                f"conversation above is inherited from {item.source}, not your own "
                f"history. Continue as agent {agent_id}."
            ),
            tag=NoteTag.LIFECYCLE_FORK,
            created_at=datetime.now(UTC),
        )
    )
    st.new_msgs.extend(fork_notes())


async def dispatch_batch(
    ctx: AvaContext,
    state: _state.AgentState,
    agent_id: int,
    batch: list[ClaimedInbound],
    routing: _Routing,
    st: _BatchState,
) -> None:
    """Dispatch every claimed inbound through its handler.

    Routing-guarded kinds (TERMINATE, RESTART, RESURRECT, COMPACT_REQUEST)
    are skipped when *routing.is_winner* returns False — one guard, not
    three scattered across the loop body. Multiple resurrect rows represent
    repeated failed wakes, so only the newest one renders a lifecycle marker.
    """
    latest_resurrect_id = max(
        (item.id for item in batch if item.kind == InboundKind.RESURRECT),
        default=None,
    )
    for item in batch:
        kind = item.kind
        if kind in _ROUTING_KINDS and not routing.is_winner(item):
            continue
        # A fresh non-task inbound starts unassociated work. A task system
        # note below is the only writer that can establish attribution again.
        if kind != InboundKind.SYSTEM_NOTE:
            st.active_task_id = None
        if kind == InboundKind.CHAT:
            await _handle_chat(item, st)
        elif kind == InboundKind.SYSTEM_NOTE:
            await _handle_system_note(item, st)
        elif kind == InboundKind.COMPACT_SUMMARY:
            await _handle_compact_summary(item, st)
        elif kind == InboundKind.COMPACT_REQUEST:
            await _handle_compact_request(ctx, state, item, st)
        elif kind == InboundKind.CANCEL:
            await _handle_cancel(ctx, agent_id, st)
        elif kind == InboundKind.HEARTBEAT:
            await _handle_heartbeat(ctx, agent_id, item, st, state)
        elif kind == InboundKind.TERMINATE:
            await _handle_terminate(ctx, item, st)
        elif kind == InboundKind.RESTART:
            await _handle_restart(ctx, agent_id, item, st)
            await _handle_restart_stateful(state, item, st)
        elif kind == InboundKind.RESTART_COMPLETED:
            await _handle_restart_completed(item, st)
        elif kind == InboundKind.RESURRECT:
            if item.id == latest_resurrect_id:
                await _handle_resurrect(item, st)
        elif kind == InboundKind.FORK:
            await _handle_fork(agent_id, item, st, state)
        else:
            raise ValueError(f"Unknown inbound kind: {kind!r} (id={item.id})")
    if len(st.task_ids) > 1:
        # Claim consumes the whole batch into one LLM turn. More than one task
        # note therefore has no faithful task-level attribution.
        st.active_task_id = None
