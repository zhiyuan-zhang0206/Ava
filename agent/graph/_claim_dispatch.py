"""Per-kind inbound dispatch for the claim node.

Extracted from agent/graph/_claim.py (Task #1006 split — the kind-dispatch axis).
Holds the dispatch-pass accumulator (_BatchState), the lifecycle marker
renderers (_by_who / _ts_prefix / _render_restart_completed_marker), one
handler per inbound kind, and the routing-guarded dispatch loop
(dispatch_batch).

Behavior (preserved verbatim from the original claim module):
- chat: append HumanMessage (envelope wrap)
- compact_summary: written by agent ava.self.compact(summary) → wipe the whole
  history and replace with [system prompt, summary] (no raw tail)
- compact_request: framework / user "/compact" triggered → run Compaction LLM
  to generate summary, then replace
- terminate: append "[system ts] You are terminated by {source}" lifecycle
  marker, goto END
- restart: only mark agents.status 'restarting' + goto END (**does not**
  append message). restarter daemon INSERTs 'restart_completed' on respawn for
  the new process, and the new process's claim appends the lifecycle marker
  (correct tense). The committed halted flag records whether the agent was
  idle at this point (excluding self sources — those have an in-flight
  intention)
- restart_completed: append "[system ts] You have been restarted by {source}"
  lifecycle marker; system:update produces "updated and restarted" wording (a
  cluster rollout). If the agent was idle pre-restart (halted=True survived)
  and the batch holds nothing else, commit the marker and re-enter claim to
  keep waiting — no LLM turn is spent acknowledging the restart
- resurrect: append "[system ts] You have been resurrected by {source}"
  lifecycle marker
- fork: append "[system ts] You have been forked from {source}..." lifecycle
  marker stating the new agent's own id (inherited history reads as the source
  agent's identity; the marker corrects "who am I" before the first turn)
- Unknown kind: ValueError fail-fast

Routing-guarded kinds (TERMINATE, RESTART, RESURRECT, COMPACT_REQUEST) are
skipped when _Routing.is_winner returns False — one guard, not three scattered
across the loop body.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

from langchain_core.messages import HumanMessage
from psycopg_pool import AsyncConnectionPool

from agent import state as _state
from agent.db import ClaimedInbound, mark_agent_status
from agent.graph._chat_inbound import build_chat_inbound
from agent.graph._context import AvaContext
from agent.graph._context_notes import fork_notes
from agent.graph._nodes import BEFORE_LLM, END
from agent.hooks.compact import (
    COMPACT_MAX_ATTEMPTS,
    CompactionFailedError,
    conversation_messages,
    generate_summary,
)
from agent.messages import NoteTag, system_note_message
from shared.agents import AgentStatus
from shared.config import now_timestamp, settings
from shared.inbound import InboundKind
from shared.live_events import Cancelled
from shared.log import logger
from shared.message_kwargs import AvaMsgType

from ._claim_routing import _ROUTING_KINDS, ClaimGoto, _Routing


@dataclass
class _BatchState:
    """Mutable accumulator carried through one dispatch pass.

    Initialised with the inheritable channel values from AgentState; mutated
    in-place by the per-kind handlers during dispatch_batch().
    """

    new_msgs: list[HumanMessage] = field(default_factory=list)
    next_goto: ClaimGoto = BEFORE_LLM
    compact_payload: tuple[str, str] | None = None  # (summary_text, compact_kind)
    cancelled: bool = False
    restart_preserves_idle: bool = False
    restart_cas_applied: bool = False
    update_initiated: bool = False
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
    st.committed_chat_ids.append(item.id)


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
) -> None:
    """HEARTBEAT: check-in delivered as a system note, plus page liveness.

    A cluster rollout rebuilds the session tree the page servers live in,
    killing every page server while the agents stay alive — boot recovery
    never runs (Task #973). Probing on each heartbeat (default 5 min) makes
    a live agent self-heal its pages: dead serve_dir pages are re-served,
    dead no-dir pages are closed. Best-effort; reconcile never raises.
    """
    st.new_msgs.append(
        system_note_message(
            content=f"{_ts_prefix()}{item.content}",
            tag=NoteTag.HEARTBEAT,
            created_at=datetime.now(UTC),
        )
    )
    assert ctx.ops_pool is not None, "_handle_heartbeat requires ctx.ops_pool"  # noqa: S101
    from agent.startup import reconcile_open_pages

    await reconcile_open_pages(ctx.ops_pool, agent_id, event_publisher=ctx.event_publisher)


async def _handle_terminate(
    item: ClaimedInbound,
    st: _BatchState,
) -> None:
    """TERMINATE: append lifecycle marker, route to END.

    Only called when this terminate IS the routing winner (guard in
    dispatch_batch). Vetoed terminates never reach here.
    """
    st.new_msgs.append(
        system_note_message(
            content=f"{_ts_prefix()}You are terminated by {_by_who(item.source)}",
            tag=NoteTag.LIFECYCLE_TERMINATE,
            created_at=datetime.now(UTC),
        )
    )
    st.next_goto = END


async def _flip_to_restarting(pool: AsyncConnectionPool, agent_id: int) -> None:
    """CAS the row running->restarting, tolerating a concurrent lifecycle op.

    A lost CAS used to raise RuntimeError straight out of the graph and kill
    the process (agent 2147, 2026-08-03: a lost CAS during a network outage
    crashed the agent, and a crash mid-outage cannot be resurrected -- 4h
    dead, audit #689 G2). The realistic loser is a hibernate swap-out
    (SIGUSR1 parks the row 'hibernating' between the claim's running-mark and
    this CAS) or a re-entered claim that left the row 'idling'. Adapt instead
    of dying: re-read the row and retry the flip from the actual live state
    (running or idling -- the restarter respawns 'restarting' rows regardless
    of which it came from); any other state (terminated / hibernating /
    allocated / starting) means another op owns the row, so the restart is
    moot -- log and let the process END normally.
    """
    try:
        await mark_agent_status(
            pool, agent_id, AgentStatus.RESTARTING, expected_from=AgentStatus.RUNNING
        )
        return
    except RuntimeError:
        # CAS lost -- a concurrent lifecycle op (hibernate swap-out, a
        # re-entered claim's idle flip) moved the row between the claim's
        # running-mark and this CAS. Re-read and adapt instead of crashing:
        # a crash during a network outage cannot be resurrected (agent 2147,
        # 2026-08-03: 4h dead; audit #689 G2).
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT status FROM agents_meta WHERE id = %s", (agent_id,))
            row = await cur.fetchone()
        actual = row[0] if row else None
        if actual is not None and actual in (AgentStatus.RUNNING, AgentStatus.IDLING):
            try:
                await mark_agent_status(
                    pool, agent_id, AgentStatus.RESTARTING, expected_from=actual
                )
                return
            except RuntimeError:
                actual = None  # moved again mid-retry -- treat as foreign
        if actual != AgentStatus.RESTARTING:
            logger.warning(
                "restart CAS lost for agent {agent_id} (actual status {actual}) -- "
                "another lifecycle op owns the row; restart not re-queued",
                event="restart_cas_lost",
                agent_id=agent_id,
                actual=actual,
            )


async def _handle_restart(
    ctx: AvaContext,
    agent_id: int,
    item: ClaimedInbound,
    st: _BatchState,
) -> None:
    """RESTART: flip status to 'restarting', route to END.

    Does NOT append a message — the new process writes the lifecycle marker
    via RESTART_COMPLETED.  Deduplicates CAS across multiple restarts in one
    batch.  Only called when this restart IS the routing winner.
    """
    if not st.restart_cas_applied:
        assert ctx.ops_pool is not None, "_handle_restart requires ctx.ops_pool"  # noqa: S101
        await _flip_to_restarting(ctx.ops_pool, agent_id)
        st.restart_cas_applied = True
        if item.source == "self":
            from agent.db import schedule_self_respawn

            schedule_self_respawn(agent_id)

    st.next_goto = END


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


async def _handle_fork(
    agent_id: int,
    item: ClaimedInbound,
    st: _BatchState,
) -> None:
    """FORK: append identity marker + graft corrected notes.

    Fork copied the source agent's full history; without a marker the model
    keeps the source's identity.  State the new identity explicitly.
    """
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
    three scattered across the loop body.
    """
    for item in batch:
        kind = item.kind
        if kind in _ROUTING_KINDS and not routing.is_winner(item):
            continue
        if kind == InboundKind.CHAT:
            await _handle_chat(item, st)
        elif kind == InboundKind.COMPACT_SUMMARY:
            await _handle_compact_summary(item, st)
        elif kind == InboundKind.COMPACT_REQUEST:
            await _handle_compact_request(ctx, state, item, st)
        elif kind == InboundKind.CANCEL:
            await _handle_cancel(ctx, agent_id, st)
        elif kind == InboundKind.HEARTBEAT:
            await _handle_heartbeat(ctx, agent_id, item, st)
        elif kind == InboundKind.TERMINATE:
            await _handle_terminate(item, st)
        elif kind == InboundKind.RESTART:
            await _handle_restart(ctx, agent_id, item, st)
            await _handle_restart_stateful(state, item, st)
        elif kind == InboundKind.RESTART_COMPLETED:
            await _handle_restart_completed(item, st)
        elif kind == InboundKind.RESURRECT:
            await _handle_resurrect(item, st)
        elif kind == InboundKind.FORK:
            await _handle_fork(agent_id, item, st)
        else:
            raise ValueError(f"Unknown inbound kind: {kind!r} (id={item.id})")
