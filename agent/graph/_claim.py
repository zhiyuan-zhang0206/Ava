"""claim node: long await + dispatch by inbound kind.

Replaces V1's transitional entry_node + loop.py's pick_thread/claim/mark_done
flow. Per framework-rearchitecture v2 unified gateway design: all agent
control signals pass through the inbound table, dispatched here by kind (see
decisions/2026-05-02-self-cycling-langgraph.md +
decisions/2026-04-26-inbound-queue.md).

This module is the pipeline orchestrator; the per-axis logic lives in
co-located modules (Task #1006 split — the original 979-line file was divided
by the batch-claim / kind-dispatch / lifecycle-routing axes, behavior preserved):

- `_claim_batch.py`     — batch acquisition: idle wait loop, batch claim, idle trim, chat deferral
- `_claim_routing.py`   — lifecycle routing: ClaimGoto vocabulary + batch winner resolution
- `_claim_dispatch.py`  — per-kind dispatch: batch state, lifecycle markers, handlers, dispatch loop
- `_claim_decide.py`    — decision: post-dispatch short-circuit rules → single Command
- `_claim_present.py`   — display: SSE publishing for the frontend timeline

Pipeline (see _claim_node_impl): container early-return → first SELECT →
idle wait (if halted / no conversation) → routing → dispatch → decide → END
snapshot. The full dispatch-by-kind behavior spec and the routing winner
semantics are preserved in the submodule docstrings above.

after_exec **always** routes to claim now (no longer only when halted=True),
ensuring user chat in the middle of a multi-step loop can also be promptly
claimed and merged into the next LLM round.

Auto-compact logic now lives in `agent/hooks/compact.py:auto_compact_before_llm`
as a builtin before_llm hook — claim no longer does maintenance work, only
inbound dispatch.

Deps injected via `runtime.context: AvaContext` (see agent/graph/_context.py).
agent_id read from RunnableConfig (LangGraph checkpointer standard).

State type hint key design (`state: _state.AgentState` + `from __future__ import
annotations`): see `agent/graph/_exec.py` module docstring last paragraph —
LangGraph narrows channels by the node's first param type hint; directly
importing `AgentState` captures the BaseAgentState alias and loses all plugin
fields; using the module attribute + deferred annotation evaluation picks up
the dynamic class rebound by build_agent_state.

Refactored (2026-08): _Routing + resolve_routing, _BatchState + per-kind
handlers, _Outcome + decide extracted; display logic moved to _claim_present.py;
file split by axis under Task #1006. cc 70 → ~8-10 per module.
"""

from __future__ import annotations

from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime
from langgraph.types import Command

from agent import state as _state
from agent.db import claim_inbound_batch
from agent.graph._attach_drain import build_attach_drain
from agent.graph._claim_decide import decide
from agent.graph._claim_dispatch import _BatchState, dispatch_batch
from agent.graph._claim_present import (
    publish_end_timeline_snapshot,
    publish_inbound_committed,
)
from agent.graph._claim_routing import ClaimGoto, resolve_routing
from agent.graph._context import AvaContext, agent_id_from_config
from agent.graph._node_log import flush_node_exit_aggregate, node_lifecycle
from agent.graph._nodes import BEFORE_LLM, CLAIM, END
from agent.impersonation import claim_gate
from agent.inbound_ownership import RuntimeOwnershipLostError
from agent.messages import has_conversation

# Names moved to co-located submodules during the Task #1006 split, re-exported
# here so existing `from agent.graph._claim import ...` call sites (tests) keep
# working unchanged. New code should import from the submodule that owns them.
from ._claim_dispatch import _by_who as _by_who
from ._claim_dispatch import _handle_heartbeat as _handle_heartbeat
from ._claim_dispatch import _render_restart_completed_marker as _render_restart_completed_marker


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
    control = await claim_gate(state, agent_id, ctx)
    if control is not None:
        return control  # pyright: ignore[reportReturnType]

    # ── First SELECT: try uncontended claim before pub/sub wait ──
    try:
        batch = await claim_inbound_batch(ctx.ops_pool, agent_id)
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
        if state.halted or not has_conversation(state.messages) or state.circuit.parks_idle:
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
        return Command[ClaimGoto](
            update={"halted": False, "turn_active": True},
            goto=BEFORE_LLM,
        )

    if not batch:
        return Command[ClaimGoto](goto=CLAIM)

    # ── Routing: resolve winner once ──
    routing = await resolve_routing(ctx, agent_id, batch)  # pyright: ignore[reportUnknownArgumentType]

    # ── Dispatch: run every item through its handler ──
    st = _BatchState(
        update_initiated=state.update_initiated,
        active_task_id=state.active_task_id,
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
    update["active_task_id"] = st.active_task_id
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
    # of the turn would never appear. The predicate mirrors _claim_node_impl's
    # idle decision exactly (same expression, same state). If a batch wakes the
    # claim right after, the snapshot is still a legal view of the committed
    # state — the next node's incremental snapshot covers the new messages.
    will_idle = state.halted or not has_conversation(state.messages)
    async with node_lifecycle(
        CLAIM,
        messages=state.messages,
        ops_pool=runtime.context.ops_pool,
        event_publisher=event_publisher,
        agent_id=agent_id,
        full_window=will_idle,
    ):
        return await _claim_node_impl(state, runtime, config)
