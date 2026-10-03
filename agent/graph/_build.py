"""build_graph: assemble 8-Node self-cycling topology, all Command(goto=) routing.

Deps (ops_pool / llm / event_publisher) are injected via `Runtime[AvaContext]` —
build_graph does not take them; the caller passes them via
`graph.ainvoke(..., context=AvaContext(...))`. Node functions access them via
`runtime.context.X`.

At startup, `load_extensions()` reads `$AVA_HOME/plugins_config.json` and
imports the `plugin.py` of every enabled plugin, by path — builtin and external
alike, one loop — followed by each plugin's `agent_runtime.py` face (state
fields, hooks, prompt sections). The loader lives in `agent.extensions` (task
#3633 moved it off this module so surface-only processes never need the graph
kernel); this module calls it directly for the graph build. The
import mechanics and the fail-soft contract live in `ava.sdk_surface.plugin_loader`
(`load_plugin_module` / `safe_load_plugin_module`), the same primitives
`ava.sdk_surface.plugin_loader.scan_and_load` uses at host boot, so both
production load paths agree on module name, package context, `sys.modules`
identity, and containment.
A repeat call re-executes the module already in `sys.modules` rather than
binding a new one, so a plugin module's identity is stable for the life of the
process. Layer A wrap monkey-patches the process's ava module; the exec child
re-runs the plugin surface load at its own boot, so agent code there sees the
wrapped version too. Config decides what is imported; not imported = not
registered.
"""

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from agent.extensions import load_extensions
from agent.hooks import make_hook_runner
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
from base.agents.context import AvaContext

from ._init_context import init_context_node
from .claim.node import claim_node
from .exec.node import exec_node
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
    checkpointer: BaseCheckpointSaver | None = None,
) -> CompiledStateGraph[BaseAgentState, AvaContext, BaseAgentState, BaseAgentState]:
    """Build 8-Node self-cycling graph — deps injected via ainvoke(context=AvaContext);
    this function only takes checkpointer.

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

    type: ignore[arg-type] — langgraph add_node stub narrows action to the
    single-arg StateNode protocol, but runtime accepts the (state, runtime,
    config) multi-arg signature. Functionally correct, just stub doesn't narrow.
    """
    load_extensions()

    # Register built-in hooks. Must run after load_extensions() because
    # clear_plugin_registrations() (called at the top of load_extensions)
    # clears all hooks including built-in ones. Repair registers first:
    # it guards the message history every hook after it (compact's
    # force-compact summarization) may feed to an LLM.
    from agent.hooks.capabilities import register_capabilities_hooks
    from agent.hooks.compact import register_compact_hooks
    from agent.hooks.repair import register_repair_hooks

    register_repair_hooks()
    register_compact_hooks()
    # Last: the capability-index drift check appends a note to whatever history
    # survives repair's guard and compact's possible full replacement.
    register_capabilities_hooks()

    g = StateGraph(build_agent_state(), context_schema=AvaContext)
    g.add_node(  # type: ignore[arg-type]
        AFTER_INIT, protect_native_hooks(make_hook_runner("after_init", default_next=INIT_CONTEXT))
    )
    g.add_node(INIT_CONTEXT, protect_native_hooks(init_context_node))  # type: ignore[arg-type]
    g.add_node(CLAIM, claim_node)  # type: ignore[arg-type]
    g.add_node(  # type: ignore[arg-type]
        BEFORE_LLM, protect_native_hooks(make_hook_runner("before_llm", default_next=LLM))
    )
    g.add_node(LLM, llm_node)  # type: ignore[arg-type]
    g.add_node(  # type: ignore[arg-type]
        BEFORE_EXEC, protect_native_hooks(make_hook_runner("before_exec", default_next=EXEC))
    )
    g.add_node(EXEC, protect_native_hooks(exec_node))  # type: ignore[arg-type]
    g.add_node(  # type: ignore[arg-type]
        AFTER_EXEC,
        protect_native_hooks(make_hook_runner("after_exec", default_next=_after_exec_default_next)),
    )
    g.add_edge(START, AFTER_INIT)
    if checkpointer is None:
        checkpointer = MemorySaver()
    return g.compile(checkpointer=checkpointer)  # pyright: ignore[reportUnknownMemberType]
