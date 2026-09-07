"""Trace↔checkpoint correlation after each turn (trace v2, task #792 group C).

Spans are metadata-only; turn content (messages incl. the system prompt)
lives in the checkpoints table. `attach_trace_checkpoint_ref` stamps the
two one-way links that make either direction resolvable, right after the
turn's ainvoke commits its checkpoint:

- the root span (still current — the caller runs inside `turn_span`)
  gains `ava.checkpoint_id=<checkpoint_id>`, so any OTel viewer can jump
  from a trace to its checkpoint (best-effort since #1964: the root is ended
  at turn start, so the attribute only lands while recording — production
  roots are already ended and skip it silently);
- the checkpoint row's metadata gains `trace_id=<trace_id>`, so the
  gateway endpoint `/api/agents/{id}/traces/{trace_id}/messages` resolves
  content by trace id.

Failure-tolerant by design: any failure (DB hiccup, span context invalid,
checkpoint not yet readable) just loses that one turn's link — tracing and
checkpointing themselves are never on this path.

"""

from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph

from agent.state import BaseAgentState
from shared.context import AvaContext


async def attach_trace_checkpoint_ref(
    graph: CompiledStateGraph[BaseAgentState, AvaContext, BaseAgentState, BaseAgentState],
    ctx: AvaContext,
    agent_id: int,
) -> None:
    """Correlate the just-finished turn's OTel trace with the checkpoint it
    committed. See the module docstring for the two links and the
    failure-tolerance contract.
    """
    from opentelemetry import trace as otel_trace

    span = otel_trace.get_current_span()
    span_ctx = span.get_span_context()
    if not span_ctx.is_valid:
        return
    config: RunnableConfig = {"configurable": {"thread_id": str(agent_id)}}
    try:
        state = await graph.aget_state(config)
        cfg = state.config.get("configurable") or {}
        checkpoint_id: Any = cfg.get("checkpoint_id")
        if checkpoint_id is None:
            return
    except Exception:
        # No readable checkpoint yet (fresh thread, first super-step) — the
        # next turn's commit will carry the link.
        return
    # The span-side write only lands while the span is still recording. The
    # turn root is a placeholder ended at turn START (#1964), so in
    # production this is skipped silently (an ended span would drop the
    # attribute with a warning line every turn). The link that matters — the
    # checkpoints.metadata.trace_id stamp below, read by the gateway trace
    # endpoint — is DB-side and unaffected.
    if span.is_recording():
        span.set_attribute("ava.checkpoint_id", checkpoint_id)
    from shared.checkpoint import attach_trace_to_checkpoint

    await attach_trace_to_checkpoint(
        ctx.ops_pool,
        thread_id=str(agent_id),
        checkpoint_id=checkpoint_id,
        trace_id=format(span_ctx.trace_id, "032x"),
    )
