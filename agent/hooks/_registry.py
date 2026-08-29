"""Graph-edge hook registry — base class + registry for the 3 hook container
Nodes + runner factory.

A hook is a subclass of `Hook` (PyTorch `nn.Module` style): override the typed
`__call__`, and — because the base pins the signature every hook point shares —
pyright (strict) flags an incompatible override. Plugins attach an *instance* to
the corresponding list via `register_before_llm` / `register_before_exec` /
`register_after_exec`; `make_hook_runner(name, default_next)` snapshots this list
into the closure at graph build time. An instance is callable, so the runner
invokes each with `hook(state, runtime, config)` exactly as it called the bare
functions of the earlier design — call sites are unchanged.

Hook signature (aligned with the business node three-arg signature):

    class MyHook(Hook):
        async def __call__(
            self,
            state: _state.AgentState,
            runtime: Runtime[AvaContext],
            config: RunnableConfig,
            /,
        ) -> dict | None: ...

Return dict → state update (same as LangGraph standard reducer format);
return None → no-op. If the returned dict has 'goto': str → override default
routing (the hook container Node looks at update["goto"] after running all
hooks to decide next; if not set, passes through the business Node's default).
All three hook points share this one shape (no per-point generic): the return
contract is identical, only which list an instance registers into differs.

An instance may carry per-hook state/config in `__init__` — the registry holds
instances, so a hook that needs a threshold, a counter, or a handle keeps it on
`self` rather than in a module global.

`config` is used to get agent_id via
`agent.graph._context.agent_id_from_config(config)` — hooks that need agent
identity for things like INSERT inbound / marking agent-scoped events use
this. Hooks that don't need agent_id (such as pure state-watching
auto_compact) also take config but don't read; signature unified to keep
the protocol simple.

No priority, no timeout, no try-except isolation — hooks that raise blow up (fail-fast).

State type hint key design (`state: _state.AgentState` + `from __future__ import
annotations`): see `agent/graph/_exec.py` module docstring last paragraph —
the `run` returned by `make_hook_runner` is a LangGraph-registered node;
like claim/llm/exec, it relies on the first-param type hint to determine
what state schema LangGraph passes the hook. Directly importing `AgentState`
captures the BaseAgentState alias and makes the hook receive a state without
plugin fields; using module attribute + deferred annotation evaluation picks
up the dynamic class rebound by build_agent_state.
"""

from __future__ import annotations

import time
import weakref
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime
from langgraph.types import Command

from agent import state as _state
from agent.nodes import NodeName
from shared import plugin_activation, plugin_contributions
from shared.context import AvaContext, agent_id_from_config
from shared.log import logger
from shared.plugin_context import current_plugin_name

HookName = Literal["before_llm", "before_exec", "after_exec", "after_init"]


class Hook(ABC):
    """Base class for a graph-edge hook — subclass and override `__call__`.

    The base pins the typed call signature every hook point shares, so a subclass
    with an incompatible `__call__` (wrong param type, wrong arity, wider return)
    is a pyright `reportIncompatibleMethodOverride` error under strict mode — the
    signature contract is enforced statically, not by convention. A subclass that
    forgets to override `__call__` cannot be instantiated (ABC abstract-method
    enforcement).

    Arguments are positional-only (`/`): pyright checks parameter *types* by
    position, not names, so an override may name them freely (`_runtime` to
    signal "unused", etc.).

    An instance is the unit of registration and is directly callable, so the
    runner invokes it with `hook(state, runtime, config)` — the same call the
    earlier function-valued design used.
    """

    @abstractmethod
    async def __call__(
        self,
        state: _state.AgentState,
        runtime: Runtime[AvaContext],
        config: RunnableConfig,
        /,
    ) -> dict | None: ...

    @property
    def name(self) -> str:
        """Identity used in diagnostics (e.g. the co-write clash message).
        Defaults to the class name; override for a more specific label."""
        return type(self).__name__


# Global registry — `make_hook_runner` snapshots this list into closure at
# graph build time; hooks registered later at runtime are also seen by old
# runners (the list is the same object).
HOOKS: dict[HookName, list[Hook]] = {
    "before_llm": [],
    "before_exec": [],
    "after_exec": [],
    "after_init": [],
}


# hook instance -> the plugin that registered it, so the runner can attribute a
# firing without `HOOKS` (or the `Hook` protocol) growing a field. Weak keys:
# an entry disappears with the hook instance, so a plugin reload that rebuilds
# its hooks does not accumulate dead attributions. Framework hooks register
# outside a `PluginContext` and are absent here — which is exactly the gate
# `plugin_activation.record` applies, so framework hooks stay untelemetered.
_HOOK_PLUGIN: weakref.WeakKeyDictionary[Hook, str] = weakref.WeakKeyDictionary()


def _register(hook_name: HookName, hook: Hook) -> None:
    """Append to the hook point's list and attribute the registration to the
    importing plugin (a no-op outside a `PluginContext`, i.e. for the framework's
    own hooks) — `HOOKS` holds bare instances, so `ava plugins inspect` reads the
    attribution off the ledger and the runner reads it off `_HOOK_PLUGIN`."""
    HOOKS[hook_name].append(hook)
    plugin_contributions.record(
        "hooks", hook_name, detail=f"{type(hook).__module__}.{type(hook).__qualname__}"
    )
    plugin = current_plugin_name()
    if plugin is not None:
        _HOOK_PLUGIN[hook] = plugin


def register_before_llm(hook: Hook) -> None:
    """Register a before_llm hook — runs after claim completes, before calling LLM."""
    _register("before_llm", hook)


def register_before_exec(hook: Hook) -> None:
    """Register a before_exec hook — runs after LLM completes, before subprocess exec."""
    _register("before_exec", hook)


def register_after_exec(hook: Hook) -> None:
    """Register an after_exec hook — runs after subprocess exec completes, before moving to the next node."""
    _register("after_exec", hook)


def register_after_init(hook: Hook) -> None:
    """Register an after_init hook — runs once after state is loaded from checkpoint, before claim."""
    _register("after_init", hook)


def make_hook_runner(
    hook_name: HookName,
    default_next: NodeName | Callable[[_state.AgentState], NodeName],
) -> Callable[
    [_state.AgentState, Runtime[AvaContext], RunnableConfig], Awaitable[Command[NodeName]]
]:
    """Generate a LangGraph Node function that runs all registered hooks for the given hook_name.

    Routing model: the graph topology only has the START → claim edge;
    everything else relies on each Node returning Command(goto=...) for
    dynamic routing. The hook container Node's default next step is passed
    in via the `default_next` parameter at build_graph time; hooks that
    want to override routing set "goto" in the update dict → runner
    prioritizes the hook's goto, falls back to default.

    `default_next` can be:
    - **NodeName**: fixed next step (before_llm → "llm" / before_exec → "exec" both use this)
    - **Callable[[AgentState], NodeName]**: dynamically decide based on state
      (after_exec uses this: `lambda s: "claim" if s.halted else "before_llm"`).
      The state callable sees is the pre-hook state — LangGraph's reducer
      only applies after Node return.

    Runner behavior:
    - for-loop runs all hooks in HOOKS[hook_name]; each hook receives
      (state, runtime, config) three args — `config` is automatically passed
      in by LangGraph when calling the Node
    - Each hook returning dict → merged into update. Two hooks writing the
      **same key** in one pass are reducer-aware: a key carrying a non-trivial
      reducer (e.g. `messages` → add_messages) merges both hooks' values via
      that reducer; a key WITHOUT a reducer raises RuntimeError (fail-loud),
      since a silent last-wins merge would let one hook's update clobber
      another's (e.g. ava_compact's full-history replacement swallowing a
      sibling's appended note). Reducerless same-key co-writes are a
      coordination bug to surface, not a merge to resolve — hooks that share a
      node sequence the collision themselves (the sibling defers when the
      other will write).
    - Each hook returning None → skip
    - A **plugin** hook returning a non-empty dict also emits one
      `plugin_activation` event naming the keys it wrote
      (`shared.plugin_activation`) — a pure side channel, and silent for
      framework hooks and for `None` returns.
    - Final return Command(update=update_minus_goto, goto=next_node)
    """
    # node_lifecycle wraps with enter/exit events + publish a timeline snapshot —
    # hook container node is part of the graph; death observability same as
    # claim/llm/exec; `hook_name` matches NodeName (before_llm / before_exec /
    # after_exec), used directly as the node name to emit.
    from agent.graph._node_log import (
        node_lifecycle,  # local import to avoid top-level cycle (graph→hooks→graph)
    )

    hooks = HOOKS[hook_name]

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
            ops_pool=runtime.context.ops_pool,
            event_publisher=event_publisher,
            agent_id=agent_id_from_config(config),
        ):
            update: dict[str, Any] = {}
            key_writer: dict[str, str] = {}  # key -> name of the hook that set it
            # Snapshot state schema's model_fields once for reducer lookup during
            # co-write detection.  Keys with a non-trivial reducer (e.g. messages →
            # add_messages) allow multiple hooks to co-write without clobbering —
            # the reducer merges the values rather than overwriting.
            _model_fields = type(state).model_fields  # pyright: ignore[reportUnknownMemberType]
            # Per-hook timings — one event per hook-runner pass, so a slow
            # before_llm / before_exec node can be attributed to the hook that
            # ate the time without a live debugger (the node span alone is a
            # black box: no sub-spans, no events).
            timings: list[tuple[str, float]] = []
            for hook in hooks:
                started = time.monotonic()
                result = await hook(state, runtime, config)
                timings.append((hook.name, time.monotonic() - started))
                if not result:
                    continue
                # Activation telemetry (philosophy §6): a plugin hook that
                # returned a state update did something to this turn — record
                # which keys it touched. A None / {} return is pure observation
                # and stays free. Plugin state-field writes travel through this
                # dict, so the `state` surface needs no separate probe.
                plugin_activation.record(
                    _HOOK_PLUGIN.get(hook),
                    "hooks",
                    hook_name,
                    detail=f"{hook.name} wrote {','.join(sorted(result))}",  # pyright: ignore[reportUnknownArgumentType]
                )
                for key, value in result.items():
                    if key in update:
                        prior = key_writer[key]
                        this = hook.name
                        # Check state schema for a non-trivial reducer on this key.
                        field_info = _model_fields.get(key)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
                        reducer = None
                        if field_info is not None:
                            for m in field_info.metadata:  # pyright: ignore[reportUnknownMemberType]
                                if callable(m) and not isinstance(m, type):
                                    reducer = m
                                    break
                        if reducer is None:
                            raise RuntimeError(
                                f"{hook_name} hooks {prior!r} and {this!r} both wrote "
                                f"key {key!r} in one pass — a later write would silently "
                                f"clobber the earlier one. Hooks sharing a node must not "
                                f"co-write a key; sequence the collision (the sibling "
                                f"defers when the other will write)."
                            )
                        # Non-trivial reducer: merge values (e.g. add_messages for
                        # 'messages' appends messages from both hooks).
                        update[key] = reducer(update[key], value)  # pyright: ignore[reportUnknownArgumentType]
                    else:
                        key_writer[key] = hook.name
                        update[key] = value  # pyright: ignore[reportUnknownArgumentType]
            if timings:
                # Skipped on an empty pass (no hooks registered) — an event
                # with nothing to attribute is noise.
                logger.info(
                    "[hook {node}] {durations}",
                    node=hook_name,
                    durations=", ".join(f"{name} {ms * 1000:.1f}ms" for name, ms in timings),
                    event="hook_timing",
                    hook_ms={name: round(ms * 1000, 1) for name, ms in timings},
                )
            if "goto" in update:
                next_node = update.pop("goto")
            elif callable(default_next):
                next_node = default_next(state)
            else:
                next_node = default_next
            return Command[NodeName](update=update, goto=next_node)

    return run
