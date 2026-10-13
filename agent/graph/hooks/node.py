"""One hook container node: the framework's hook runner inside the graph's node lifecycle.

`agent.hooks.make_hook_runner` knows how to run a hook list at one graph edge and nothing about
the graph. A container node is part of the graph, so its death observability matches claim, llm
and exec: `node_lifecycle` wraps each pass with enter/exit events and publishes a timeline
snapshot on enter. `hook_name` matches the NodeName (after_init / before_llm / before_exec /
after_exec) and is used directly as the node name to emit.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence

from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime
from langgraph.types import Command

from agent import state as _state
from agent.graph.node_log import node_lifecycle
from agent.hooks import HookName, make_hook_runner
from agent.nodes import NodeName
from base.agents.context import AvaContext, agent_id_from_config
from base.agents.history.timeline_inputs import TimelineReadInputs
from base.packages.plugins.extensions import GraphHook


def hook_node(
    hook_name: HookName,
    default_next: NodeName | Callable[[_state.AgentState], NodeName],
    hooks: Sequence[tuple[str | None, GraphHook]],
) -> Callable[
    [_state.AgentState, Runtime[AvaContext], RunnableConfig], Awaitable[Command[NodeName]]
]:
    """A LangGraph node that runs `hooks` at one edge, wrapped in the node lifecycle events."""
    runner = make_hook_runner(hook_name, default_next, hooks)

    async def run(
        state: _state.AgentState,
        runtime: Runtime[AvaContext],
        config: RunnableConfig,
    ) -> Command[NodeName]:
        event_publisher = runtime.context.event_publisher
        assert event_publisher is not None, (  # noqa: S101
            f"hook runner ({hook_name}) requires ctx.event_publisher"
        )
        async with node_lifecycle(
            hook_name,
            messages=state.messages,
            event_publisher=event_publisher,
            agent_id=agent_id_from_config(config),
            turn_progress=runtime.context.turn_progress,
            read_stall_seconds=lambda: runtime.context.require_agent().read(
                "agent", "node_stall_dump_seconds"
            ),
            timeline_inputs=TimelineReadInputs(
                runtime.context.require_clock,
                lambda: runtime.context.require_agent().read("general", "message_timestamps"),
            ),
            limit_reader=lambda: runtime.context.require_agent().read(
                "display", "timeline_default_limit"
            ),
        ):
            return await runner(state, runtime, config)

    return run
