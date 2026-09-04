"""Turn-level no-progress stall guard — one `graph.ainvoke` under a timeout.

The hosted runner's turn-level injection guard (task #2417 half 2): a graph
invocation whose turn-progress clock has shown no activity for
``AVA_HOST_TURN_NO_PROGRESS_TIMEOUT_SECONDS`` is aborted instead of hanging
forever under a live heartbeat lease — agent 2998's exact failure (claimed its
whole inbound queue, then hung inside ``graph.ainvoke`` for 3.5 hours with the
lease renewing).

Engineered as module-level functions rather than `AgentHost` methods to keep
``host.py`` inside the 800-line ceiling; also the honest shape — the guard is a
pure input/output function of (graph, agent_id, ctx, config, input) and keeps
no host state.

The guard, not a wall clock, is the timeout. A turn that keeps making progress
may legitimately run for days (the hosted design explicitly allows long
autonomous loops), so an absolute turn budget would be wrong; only silence
counts. The clock (agent/_turn_progress.py) is marked by every LangGraph node
enter, completed LLM step and streamed LLM chunk, so a long exec or a long
model stream keeps the turn alive while a graph-level hang — or a provider
hanging without ever completing a step — does not.
"""

from __future__ import annotations

import asyncio
import contextlib

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph

from agent._runloop import _emit_error_event
from agent._turn_progress import reset_turn_progress, turn_progress_age_s
from agent.state import BaseAgentState
from services.agent_host.dispatcher import (
    HostRestartRequiredError,
    TurnStallTimeoutError,
)
from shared.config import settings
from shared.context import AvaContext
from shared.log import logger
from shared.stop_timing import CANCEL_UNWIND_TIMEOUT_S

_Graph = CompiledStateGraph[BaseAgentState, AvaContext, BaseAgentState, BaseAgentState]


async def run_invocation_with_stall_guard(
    graph: _Graph,
    agent_id: int,
    ctx: AvaContext,
    config: RunnableConfig,
    input_update: dict[str, object],
) -> dict[str, object]:
    """Run one ``graph.ainvoke`` under the turn-level no-progress stall guard.

    On stall: the invocation task is cancelled inside the host's bounded unwind
    window (`CANCEL_UNWIND_TIMEOUT_S`). Unwound → `TurnStallTimeoutError` (the
    turn task ends; ``run_turn`` drops the runtime, so the next wake re-runs
    the startup reconcile and resumes from the checkpoint). Refused →
    `HostRestartRequiredError` (the daemon must exit; the supervisor restarts
    it — rescheduling beside a task that still owns the slot would violate
    one-turn-per-agent).

    An EXTERNAL cancellation (the dispatcher's stale-turn scan, force
    terminate, shutdown) lands on THIS task's awaits rather than the graph's,
    because the graph now runs one level down. It is mirrored onto the graph
    task and its unwind is awaited here, so the external cancellation keeps
    exactly the unwinding semantics it had before this guard existed — when
    the graph task WAS the turn task.
    """
    reset_turn_progress(agent_id)
    invoke_task = asyncio.create_task(
        graph.ainvoke(  # pyright: ignore[reportUnknownMemberType, reportAssignmentType]
            input_update,  # pyright: ignore[reportArgumentType, reportUnknownMemberType]
            config=config,
            context=ctx,
        ),
        name=f"turn-ainvoke-{agent_id}",
    )
    try:
        while True:
            done, _pending = await asyncio.wait(
                {invoke_task},
                timeout=settings.daemon.host_turn_progress_scan_interval_seconds,
            )
            if invoke_task in done:
                return invoke_task.result()
            age = turn_progress_age_s(agent_id)
            if age is not None and age >= settings.daemon.host_turn_no_progress_timeout_seconds:
                return await _abort_stalled_invocation(agent_id, ctx, invoke_task, age)
    except asyncio.CancelledError:
        if not invoke_task.done():
            invoke_task.cancel()
            with contextlib.suppress(BaseException):
                await invoke_task
        raise


async def _abort_stalled_invocation(
    agent_id: int,
    ctx: AvaContext,
    invoke_task: asyncio.Task[dict[str, object]],
    age_s: float,
) -> dict[str, object]:
    """Cancel a stalled invocation and classify the outcome.

    Returns the result only in the narrow race where the invocation finished
    on its own between the stall decision and the cancel landing. Raises
    `TurnStallTimeoutError` when it unwound cancelled — the abort — and
    `HostRestartRequiredError` when it refused to unwind.
    """
    logger.error(
        "hosted turn for agent {agent_id} made no progress for "
        "{no_progress_s:.0f}s — aborting the invocation; the next wake "
        "resumes from the checkpoint",
        event="host_turn_stall_timeout",
        agent_id=agent_id,
        no_progress_s=age_s,
    )
    # The "turn result failed to land" half of the mandate: one Error event on
    # this agent's SSE — the frontend shows the abort instead of silence.
    # Best-effort by design (_emit_error_event suppresses).
    _emit_error_event(
        ctx,
        agent_id,
        f"The turn was aborted after {age_s:.0f}s with no progress. "
        "The agent will resume on its next wake.",
    )
    invoke_task.cancel()
    # `asyncio.wait`, NOT `asyncio.wait_for(asyncio.shield(...))`: a shield
    # wrapping a task that gets cancelled never completes its own future —
    # the bounded unwind would hang (verified by reproduction; see the test's
    # refuses-to-unwind case for the intended refusal shape). `asyncio.wait`
    # reports the straggler instead, and never cancels the task itself.
    done, _pending = await asyncio.wait({invoke_task}, timeout=CANCEL_UNWIND_TIMEOUT_S)
    if invoke_task not in done:
        logger.error(
            "hosted turn for agent {agent_id} did not unwind within the "
            "bounded window after a stall-cancel — the daemon must exit; "
            "the supervisor restarts it",
            event="host_turn_stall_uncancellable",
            agent_id=agent_id,
        )
        raise HostRestartRequiredError(
            f"hosted turn for agent {agent_id} did not unwind after a stall-cancel"
        ) from None
    try:
        return invoke_task.result()
    except asyncio.CancelledError:
        raise TurnStallTimeoutError(agent_id) from None
