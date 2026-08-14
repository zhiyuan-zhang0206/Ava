"""The `init_context` node — sole owner of the agent's standing message head.

The head is the SystemMessage plus the ordered context notes
(`_context_notes.context_notes`), rendered in the rank order that module
documents — exec timeout, the shared memory index, the agent id, the per-agent
memory index, any preloaded skills, then whatever else plugins register (the
two memory indexes when `ava_memory` is enabled). It has to
be laid down twice in an agent's life — once when the agent first wakes with no
history, and again after every compaction, which replaces the whole window —
and both times in the same order.

Those two moments collapse into one condition. A compaction clears the history
with `RemoveMessage(REMOVE_ALL)`, so by the time this node runs the channel is
empty exactly as it is at cold start: **an empty `messages` means the context
needs establishing**. There is no mode flag and no caller-supplied intent —
a requester clears the history, parks what should follow the head in
`state.context_reset`, and routes here.

What follows the head is the `tail` of `ContextReset`: the post-compact summary
alone — a compact is a clean wipe, and chats that arrived while the turn was
in flight are re-delivered as pending inbounds by the claim node instead of
being parked here. Cold start parks nothing, so the tail is empty and the
head is the whole message list.

A fork is deliberately NOT routed here: it inherits the source agent's full
history, so its context is not established from empty. It grafts
`_context_notes.fork_notes()` onto that history instead (see `_claim`).
"""

from __future__ import annotations

from langchain_core.messages import SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime
from langgraph.types import Command

from agent import state as _state
from agent.nodes import INIT_CONTEXT, NodeName
from agent.state import CapabilitiesState, ContextReset
from shared.log import logger

from ._capabilities import indexed_skill_identifiers
from ._context import AvaContext, agent_id_from_config
from ._context_notes import context_notes
from ._node_log import node_lifecycle
from ._system_prompt import build_system_prompt


async def init_context_node(
    state: _state.AgentState,
    runtime: Runtime[AvaContext],
    config: RunnableConfig,
) -> Command[NodeName]:
    """Lay down the standing head when `messages` is empty, then route on.

    A no-op pass-through when the history is intact — the common case, since
    every graph invocation enters through here but only a cold start and the
    turn after a compaction find an empty channel.
    """
    event_publisher = runtime.context.event_publisher
    assert event_publisher is not None, "init_context requires ctx.event_publisher"  # noqa: S101
    async with node_lifecycle(
        INIT_CONTEXT,
        messages=state.messages,
        ops_pool=runtime.context.ops_pool,
        event_publisher=event_publisher,
        agent_id=agent_id_from_config(config),
    ):
        reset = state.context_reset
        if state.messages:
            # History intact — nothing to establish. A parked tail here would
            # mean a requester cleared the history and something re-populated it
            # before this node ran; drop it rather than splicing it into the
            # middle of a live conversation.
            if reset.tail:
                logger.warning(
                    "[init-context] messages non-empty ({}) with a parked tail of {} — "
                    "dropping the tail",
                    len(state.messages),
                    len(reset.tail),
                )
                return Command[NodeName](
                    update={"context_reset": ContextReset()}, goto=reset.resume
                )
            return Command[NodeName](goto=reset.resume)

        # Container mode (eval harness): no ops_pool, so no inbound queue, no
        # workspace, and nothing the notes describe. The head is the system
        # prompt alone, keeping an eval's context deterministic. Same signal
        # `_claim` uses for its container early-return.
        notes = [] if runtime.context.ops_pool is None else context_notes()
        logger.info(
            "[init-context] establishing: {} note(s) + {} tail message(s), resume={}",
            len(notes),
            len(reset.tail),
            reset.resume,
        )
        # Record what `# Capabilities` covers BEFORE rendering it. The section is
        # frozen into the SystemMessage below while `ava.skills` stays an uncached
        # live scan, so this snapshot is the only thing that can later tell the
        # two apart — see `agent/hooks/capabilities.py`. Taken first on purpose:
        # a skill landing between the two calls is then rendered but not
        # recorded, so the drift check names it one extra time, where the reverse
        # order would mark it known and drop it silently until the next
        # compaction.
        indexed = indexed_skill_identifiers()
        return Command[NodeName](
            update={
                "messages": [
                    SystemMessage(content=build_system_prompt()),
                    *notes,
                    *reset.tail,
                ],
                "capabilities": CapabilitiesState(indexed=indexed),
                # Clear the channel: the reset has been served. Written back as
                # the default so a later invocation cannot re-consume this tail.
                "context_reset": ContextReset(),
            },
            goto=reset.resume,
        )
