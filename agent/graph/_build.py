"""build_graph: assemble 8-Node self-cycling topology, all Command(goto=) routing.

Deps (ops_pool / llm / event_publisher) are injected via `Runtime[MeivaContext]` —
build_graph does not take them; the caller passes them via
`graph.ainvoke(..., context=MeivaContext(...))`. Node functions access them via
`runtime.context.X`.

Plugins reach the graph as a value: the caller loads them (`meiva.sdk_surface.extensions.load_extensions()`),
builds the `ExtensionRegistry` of what they declare (`base.packages.plugins.loading.registry.build_registry()`)
and passes it in. The graph is a function of that registry: its hooks run in the hook container
nodes, its state classes shape the dynamic `AgentState`. The loader lives in `agent.extensions`
(task #3633 moved it off this module so surface-only processes never need the graph kernel); the
import mechanics and the fail-soft contract live in `base.packages.plugins.loading.plugin_loader`
(`load_plugin_module` / `safe_load_plugin_module`): one module name, package context,
`sys.modules` identity, and containment however many times a plugin loads.
A repeat load re-executes the module already in `sys.modules` rather than
binding a new one, so a plugin module's identity is stable for the life of the
process. Layer A wrap monkey-patches the process's meiva module; the exec child
re-runs the plugin surface load at its own boot, so agent code there sees the
wrapped version too. Config decides what is imported; not imported = not
declared.
"""

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command

from agent.graph.hooks.framework import hooks_at
from agent.graph.hooks.node import hook_node
from agent.graph.llm_errors import LlmLedger
from agent.impersonation import protect_native_hooks
from agent.nodes import (
    AFTER_EXEC,
    AFTER_INIT,
    BEFORE_EXEC,
    BEFORE_LLM,
    CLAIM,
    EXEC,
    INIT_CONTEXT,
    LLM,
    NodeName,
)
from agent.state import BaseAgentState, build_agent_state
from base.agents.context import MeivaContext
from base.packages.plugins.extensions import ExtensionRegistry

from ._init_context import init_context_node
from .claim.node import claim_node
from .exec.node import exec_node
from .llm.goto import LlmGoto
from .llm.node import llm_node


def _after_exec_default_next(_state: BaseAgentState) -> NodeName:
    """After after_exec runs, always go back to claim — the claim node decides
    whether to wait or immediately continue based on pending inbound + state.halted.

    Always going back to claim, rather than only when halted=True, ensures that
    user-sent chat in the middle of a multi-step loop can be promptly claimed
    + merged into the next LLM round (instead of sitting in the inbound table
    waiting until the agent finally halts).

    Claim node behavior:
    - Pending inbound → dispatch, goto before_llm
    - No pending + halted=True / messages empty → turn boundary: goto END with
      exit_requested=False (one invocation = one turn); the runloop re-invokes
      and the fresh invocation's claim does the long wait (IDLING)
    - No pending + halted=False + messages non-empty → immediately goto before_llm (no block)
    """
    return CLAIM


def build_graph(
    checkpointer: BaseCheckpointSaver | None,
    extensions: ExtensionRegistry,
) -> CompiledStateGraph[BaseAgentState, MeivaContext, BaseAgentState, BaseAgentState]:
    """Build 8-Node self-cycling graph — deps injected via ainvoke(context=MeivaContext);
    this function takes the checkpointer and the plugin registry.

    8-Node topology:
        START → after_init → init_context → claim → before_llm → llm → before_exec → exec → after_exec
                                  ↑          ↑                                              │
                                  │          └──────────────────────────────────────────────┘
                                  │          (always back to claim; claim decides wait or
                                  │           continue based on halted + pending)
                                  └── every compaction: the requester empties `messages`,
                                      parks the post-compact tail in `context_reset`, and
                                      routes here to have the standing head re-established

    Frontend timeline sync: each node **enter** renders a timeline snapshot
    from the in-memory `state.messages` and publishes it via
    `node_log.node_lifecycle` before yield (includes msg_count =
    `len(state.messages)`); the gateway forwards it to the frontend. Rendering
    from in-memory state (not a checkpoint re-read) is race-free: LangGraph
    commits checkpoints asynchronously, so a re-read could miss the
    just-claimed inbound, but the in-memory state reflects the reducer the
    instant it applies. The msg_count protocol lets the frontend distinguish a
    single future position (LLM/exec streaming the next message) from a stale
    partial via `partial.msg_idx == msg_count`. Fallback (PR #323 streaming
    corruption → non-streaming ainvoke) does not resend streaming events;
    after commit, the next node enter's snapshot has the committed version,
    and the frontend replaces the partial's dirty tokens with the full content
    by item_id.

    **Routing entirely via Command(goto=)** — the graph build declares a single static edge
    add_edge(START, AFTER_INIT); business nodes hardcode default next; hook
    container Nodes accept a default_next parameter (NodeName or callable
    based on state) + decide based on update["goto"] override.

    Every node gets `input_schema=state_cls` (the class `build_agent_state` returns): LangGraph would
    otherwise narrow a node's state to the class its first parameter is annotated with, and that
    annotation is the static base class, which carries none of the plugins' channels.

    type: ignore[arg-type] — langgraph add_node stub narrows action to the
    single-arg StateNode protocol, but runtime accepts the (state, runtime,
    config) multi-arg signature. Functionally correct, just stub doesn't narrow.
    """
    state_cls = build_agent_state(extensions)
    g = StateGraph(state_cls, context_schema=MeivaContext)
    # What the llm node remembers per agent between tries and turns belongs to this graph: one
    # ledger per compiled graph, so two graphs in one process never share failure counts.
    ledger = LlmLedger()

    async def llm(
        state: BaseAgentState, runtime: Runtime[MeivaContext], config: RunnableConfig
    ) -> Command[LlmGoto]:
        return await llm_node(state, runtime, config, ledger=ledger)

    g.add_node(  # type: ignore[arg-type]
        AFTER_INIT,
        protect_native_hooks(
            hook_node(
                "after_init", default_next=INIT_CONTEXT, hooks=hooks_at("after_init", extensions)
            )
        ),
        input_schema=state_cls,
    )
    g.add_node(  # type: ignore[arg-type]
        INIT_CONTEXT, protect_native_hooks(init_context_node), input_schema=state_cls
    )
    g.add_node(CLAIM, claim_node, input_schema=state_cls)  # type: ignore[arg-type]
    g.add_node(  # type: ignore[arg-type]
        BEFORE_LLM,
        protect_native_hooks(
            hook_node("before_llm", default_next=LLM, hooks=hooks_at("before_llm", extensions))
        ),
        input_schema=state_cls,
    )
    g.add_node(LLM, llm, input_schema=state_cls)  # type: ignore[arg-type]
    g.add_node(  # type: ignore[arg-type]
        BEFORE_EXEC,
        protect_native_hooks(
            hook_node("before_exec", default_next=EXEC, hooks=hooks_at("before_exec", extensions))
        ),
        input_schema=state_cls,
    )
    g.add_node(EXEC, protect_native_hooks(exec_node), input_schema=state_cls)  # type: ignore[arg-type]
    g.add_node(  # type: ignore[arg-type]
        AFTER_EXEC,
        protect_native_hooks(
            hook_node(
                "after_exec",
                default_next=_after_exec_default_next,
                hooks=hooks_at("after_exec", extensions),
            )
        ),
        input_schema=state_cls,
    )
    g.add_edge(START, AFTER_INIT)
    if checkpointer is None:
        checkpointer = MemorySaver()
    return g.compile(checkpointer=checkpointer)  # pyright: ignore[reportUnknownMemberType]
