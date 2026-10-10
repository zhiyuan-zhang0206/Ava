"""claim node: long await + dispatch by inbound kind.

Replaces V1's transitional entry_node + loop.py's pick_thread/claim/mark_done
flow. Per framework-rearchitecture v2 unified gateway design: all agent
control signals pass through the inbound table, dispatched here by kind (see
docs/decisions/agents/graph/2026-05-02-self-cycling-langgraph.md +
docs/decisions/agents/messages/2026-04-26-inbound-queue.md).

This module is the pipeline orchestrator; the per-axis logic lives in
co-located modules (Task #1006 split — the original 979-line file was divided
by the batch-claim / kind-dispatch / lifecycle-routing axes, behavior preserved):

- `_batch.py`     — batch acquisition: idle wait loop, batch claim, idle trim, chat deferral
- `_routing.py`   — lifecycle routing: ClaimGoto vocabulary + batch winner resolution
- `_dispatch.py`  — per-kind dispatch: batch state, lifecycle markers, handlers, dispatch loop
- `_decide.py`    — decision: post-dispatch short-circuit rules → single Command
- `_present.py`   — display: SSE publishing for the frontend timeline

Pipeline (see _claim_node_impl): container early-return → first SELECT →
idle wait (if halted / no conversation) → routing → dispatch → decide → END
snapshot. The full dispatch-by-kind behavior spec and the routing winner
semantics are preserved in the submodule docstrings above.

after_exec **always** routes to claim now (no longer only when halted=True),
ensuring user chat in the middle of a multi-step loop can also be promptly
claimed and merged into the next LLM round.

Automatic compaction runs in the LLM node via `agent/hooks/compact.py:auto_compact_for_llm`
after the before_llm hooks commit. Claim still dispatches explicit compaction
requests and circuit-breaker rescue.

Deps injected via `runtime.context: AvaContext` (see agent/graph/_context.py).
agent_id read from RunnableConfig (LangGraph checkpointer standard).

State type hint (`state: _state.AgentState`): static only; see `agent/graph/exec/node.py` — the
graph build, not the annotation, decides which state class a node receives.

Refactored (2026-08): _Routing + resolve_routing, _BatchState + per-kind
handlers, _Outcome + decide extracted; display logic moved to _present.py;
file split by axis under Task #1006. cc 70 → ~8-10 per module.
"""

from __future__ import annotations

from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime
from langgraph.types import Command

from agent import state as _state
from agent.db import claim_inbound_batch
from agent.graph._attach_drain import build_attach_drain
from agent.graph.node_log import flush_node_exit_aggregate, node_lifecycle
from agent.impersonation import claim_gate
from agent.impersonation_handoff import resume_note_pending
from agent.messages import has_conversation
from agent.nodes import BEFORE_LLM, CLAIM, END
from agent.ownership.inbound import RuntimeOwnershipLostError
from agent.ownership.native_cancel import (
    activate_routed_work,
    halt_for_native_cancel,
    observe_bound_cancel,
)
from base.agents.context import AvaContext, agent_id_from_config
from base.agents.history.timeline_inputs import TimelineReadInputs
from base.agents.incarnation.native_work_models import NativeCancelPendingError

from ._decide import decide
from ._dispatch import _BatchState, dispatch_batch

# Names moved to co-located submodules during the Task #1006 split, re-exported
# here so existing `from agent.graph.claim.node import ...` call sites (tests)
# keep working unchanged. New code should import from the submodule that owns them.
from ._dispatch import _by_who as _by_who
from ._dispatch import _handle_heartbeat as _handle_heartbeat
from ._dispatch import _render_restart_completed_marker as _render_restart_completed_marker
from ._present import publish_end_timeline_snapshot, publish_inbound_committed
from ._routing import ClaimGoto, resolve_routing


def claim_will_idle(state: _state.AgentState) -> bool:
    """The claim's single idle predicate — impl branch and wrapper snapshot share it.

    Idle when the turn already ended (`halted`), when the window carries no
    conversation yet AND no unprocessed end-of-session note is trailing (a
    resume note is the resumed input: `deliver_handoff` appends it and the
    claim must run its first turn even on a notes-only window), or when an
    open breaker parks the agent (`parks_idle`). The wrapper's turn-end
    full-window snapshot calls this same function, so the two decisions
    cannot drift.
    """
    return (
        state.halted
        or (not has_conversation(state.messages) and not resume_note_pending(state))
        or state.circuit.parks_idle
    )


async def _claim_node_impl(
    state: _state.AgentState,
    runtime: Runtime[AvaContext],
    config: RunnableConfig,
) -> Command[ClaimGoto]:
    """Claim-node pipeline: container early-return → batch → routing → dispatch → decide.

    The original 590-line / cc=70 function is now a ~60-line / cc≈8 pipeline
    of extracted stages, each independently testable.
    """
    ctx = runtime.context

    # ── Container mode ──
    if ctx.ops_pool is None:
        if state.halted:
            return Command[ClaimGoto](goto=END)
        return Command[ClaimGoto](update={"halted": False}, goto=BEFORE_LLM)

    agent_id = agent_id_from_config(config)
    marker = await observe_bound_cancel(
        ctx.ops_pool, agent_id, incarnation=ctx.original_incarnation, work=ctx.native_work
    )
    if marker is not None:
        return Command[ClaimGoto](update=halt_for_native_cancel(marker), goto=END)
    control = await claim_gate(state, agent_id, ctx)
    if control is not None:
        return control  # pyright: ignore[reportReturnType]

    # ── First SELECT: try uncontended claim before pub/sub wait ──
    try:
        batch = await claim_inbound_batch(
            ctx.ops_pool, agent_id, incarnation=ctx.original_incarnation, work=ctx.native_work
        )
    except NativeCancelPendingError as exc:
        return Command[ClaimGoto](update=halt_for_native_cancel(exc.marker), goto=END)
    except RuntimeOwnershipLostError:
        return Command[ClaimGoto](update={"exit_requested": True}, goto=END)
    if not batch:
        # The breaker parks the agent too: an open non-overflow breaker means
        # the last LLM call was permanently rejected (billing / auth / ...) —
        # a self-initiated continue-loop (this else-less branch's normal
        # caller) would only re-fire the doomed call. Only real inbound
        # (dispatch) may attempt a call, and the breaker closes on the first
        # success (llm_node). The overflow reason is deliberately NOT parked:
        # it must keep flowing to decide()'s forced-compact arm, which runs on
        # dispatched wakes only.
        if claim_will_idle(state):
            drain = build_attach_drain(state, ctx)
            if drain is not None:
                return Command[ClaimGoto](update=drain, goto=CLAIM)
            if state.turn_active:
                # Turn boundary: one graph invocation = one turn. This
                # invocation already routed work (turn_active), and the next
                # thing to do is block for the next inbound — end the
                # invocation instead, so the runloop closes this turn's root
                # span and re-invokes on the same thread; the fresh
                # invocation's claim does the long wait. exit_requested stays
                # False: this END means "turn over", not "process exit".
                return Command[ClaimGoto](update={"turn_active": False}, goto=END)
            # The host owns the idle agent and its subscription. End this
            # invocation; the dispatcher creates another task on the next wake.
            return Command[ClaimGoto](update={"turn_active": False, "turn_idle": True}, goto=END)
        await activate_routed_work(
            ctx.ops_pool,
            state.native_work,
            incarnation=ctx.original_incarnation,
            work=ctx.native_work,
        )
        return Command[ClaimGoto](
            update={"halted": False, "turn_active": True},
            goto=BEFORE_LLM,
        )

    # ── Routing: resolve winner once ──
    await activate_routed_work(
        ctx.ops_pool, state.native_work, incarnation=ctx.original_incarnation, work=ctx.native_work
    )
    routing = await resolve_routing(ctx, agent_id, batch)  # pyright: ignore[reportUnknownArgumentType]

    # ── Dispatch: run every item through its handler ──
    st = _BatchState(
        update_initiated=state.update_initiated,
    )
    await dispatch_batch(ctx, state, agent_id, batch, routing, st)  # pyright: ignore[reportUnknownArgumentType]

    # ── Display: tell the frontend which chat inbounds were committed ──
    await publish_inbound_committed(ctx, agent_id, st.committed_chat_ids)

    # ── Decide: post-dispatch decision → single Command ──
    outcome = await decide(ctx, state, agent_id, batch, st, routing)  # pyright: ignore[reportUnknownArgumentType]

    # ── END snapshot ──
    if outcome.publish_end_snapshot:
        await publish_end_timeline_snapshot(ctx, state, agent_id, st.new_msgs)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]

    # ── Turn/exit stamping (once, for every decide outcome) ──
    # A dispatched batch means this invocation is mid-turn: stamp turn_active
    # so a later claim pass that finds nothing to do ends the invocation (the
    # turn boundary above) instead of blocking. exit_requested carries the
    # process-exit intent to the runloop; restart_requested carries the hosted
    # restart intent (drop the runtime, no exit-notify). Both key on the
    # dispatch verdict (st.next_goto == END: a terminate/restart won the
    # batch), not on the command's own goto — a compact co-batched with a
    # terminate routes through INIT_CONTEXT first and only then reaches END
    # via reset.resume.
    update = dict(outcome.command.update or {})  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    update["turn_active"] = True
    # Hosted restart sets restart_requested and must NOT set exit_requested:
    # the host drops the runtime and ends the turn task without the
    # process-exit notify. The two channels are mutually exclusive by
    # construction (restart_requested is only ever set by the hosted restart
    # branch, which never flips the row).
    update["exit_requested"] = (st.next_goto == END) and not st.restart_requested
    update["restart_requested"] = st.restart_requested
    return Command[ClaimGoto](update=update, goto=outcome.command.goto)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType, reportArgumentType]


async def claim_node(
    state: _state.AgentState,
    runtime: Runtime[AvaContext],
    config: RunnableConfig,
) -> Command[ClaimGoto]:
    """Public entry point: wraps _claim_node_impl with node-lifecycle logging.

    This is the node registered in the LangGraph state graph.  Its signature
    and return type are part of the public graph contract and MUST NOT change.
    """
    agent_id = agent_id_from_config(config)
    flush_node_exit_aggregate(agent_id)
    event_publisher = runtime.context.event_publisher
    assert event_publisher is not None, "claim_node requires ctx.event_publisher"  # noqa: S101
    # Turn-end fallback: when claim is about to block in _wait_for_batch
    # (idle), publish a FULL-WINDOW snapshot on enter — the only race-free
    # (in-memory, no checkpoint async-commit race) view of the finished turn.
    # This is what heals a frontend that missed events (SSE gap / dropped
    # deltas): the incremental snapshots in this design cover commits only, so
    # without it a reconnect GET could read a lagging checkpoint and the tail
    # of the turn would never appear. The shared `claim_will_idle` predicate is
    # the same call _claim_node_impl's idle branch makes, so the two cannot drift.
    # If a batch wakes the
    # claim right after, the snapshot is still a legal view of the committed
    # state — the next node's incremental snapshot covers the new messages.
    will_idle = claim_will_idle(state)
    async with node_lifecycle(
        CLAIM,
        messages=state.messages,
        ops_pool=runtime.context.ops_pool,
        event_publisher=event_publisher,
        agent_id=agent_id,
        turn_progress=runtime.context.turn_progress,
        read_stall_seconds=lambda: runtime.context.require_agent().read(
            "agent", "node_stall_dump_seconds"
        ),
        full_window=will_idle,
        timeline_inputs=TimelineReadInputs(
            runtime.context.require_clock,
            lambda: runtime.context.require_agent().read("general", "message_timestamps"),
        ),
        limit_reader=lambda: runtime.context.require_agent().read(
            "display", "timeline_default_limit"
        ),
    ):
        return await _claim_node_impl(state, runtime, config)
