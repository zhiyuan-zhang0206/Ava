"""build_graph: assemble 8-Node self-cycling topology, all Command(goto=) routing.

Deps (ops_pool / llm / event_publisher) are injected via `Runtime[AvaContext]` —
build_graph does not take them; the caller passes them via
`graph.ainvoke(..., context=AvaContext(...))`. Node functions access them via
`runtime.context.X`.

At startup, `_load_extensions()` reads `$AVA_HOME/plugins.json` and imports the
`plugin.py` of every enabled plugin itself, by path — builtin and external alike,
one loop, no delegation to `ava._extend.scan_and_load` (that loader is
external-only and is called once at host boot). A repeat call re-executes
the module already in `sys.modules` rather than binding a new one, so a plugin
module's identity is stable for the life of the process. Layer A wrap
monkey-patches the process's ava module; the exec child re-runs plugin loading
at its own boot, so agent code there sees the wrapped version too. Config
decides what is imported; not imported = not registered.
"""

import contextlib
import sys
import threading
from pathlib import Path

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import RetryPolicy

from agent.hooks import make_hook_runner
from agent.impersonation import protect_native_hooks
from agent.state import BaseAgentState, build_agent_state
from shared import paths
from shared import plugins_config as plugins_cfg
from shared.config import settings
from shared.config.turn_view import turn_settings

from ._claim import claim_node
from ._context import AvaContext
from ._exec import exec_node
from ._init_context import init_context_node
from ._llm import llm_node
from ._nodes import (
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

# LLM node retry policy — covers network jitter + DeepSeek server-side intermittent drift.
#
# LangGraph built-in default retry_on already covers httpx.ReadError /
# ConnectionError / 5xx HTTPStatusError network errors (agent 66: stream mid-way
# socket reset → ReadError); custom LLMStreamError subclasses (StallTimeout /
# Corrupted / Truncated / UnexpectedStopReason) are also retried via
# default_retry_on's fallthrough True.
#
# Parameters tuned beyond defaults — to handle DeepSeek API frequent rate-limit/timeout
# (agent 248):
# - max_attempts: 3 → 6 (initial + 5 retries). DeepSeek rate-limit bursts last
#   tens of seconds; 3 is not enough, 5 retries give the burst time to dissipate
#   + server recovery headroom.
# - initial_interval: 0.5s → 30.0s. Old 10s hit peak-hour rate-limit window and
#   still wasn't enough (agent 215 / 232 saw consecutive 429s); bumped to 30s
#   to give server cooldown window.
# - max_interval: 128s → 480s. Combined with initial=30s + backoff=2 exponential
#   backoff sequence: 30 → 60 → 120 → 240 → 480 (5th retry lands exactly, no cap collision).
# - backoff_factor: default 2, set explicitly — base 2 validated by all incidents, not tuning.
#
# A configurable wall-clock budget limits the sequence across attempts and
# waits. An attempt that is already in flight remains bounded by its existing
# stream timeouts; the budget prevents another provider call once it expires.
#
# DeepSeek streaming drift reference: deepseek-ai/DeepSeek-V3#1244 (V4-Pro
# tool_call intermittently stuffs wrong field, ~11% probability, mode-lock at
# prefill phase, larger context = higher trigger probability, DeepSeek team
# Open and won't fix).
#
# exec doesn't touch network; claim only does on the compact_request arm
# (one-shot Compaction LLM call, deliberately no retry policy — a failure
# there surfaces instead of retrying; the request row is already consumed).
#
# Retry-wave de-phasing (heartbeat-daemon pattern): a correlated failure
# (429 burst / provider drift / pg restart) hits every agent at the same
# moment, and a fleet-wide identical schedule (30→60→120→240→480s) would
# retry in lockstep — each wave re-synchronizes itself and keeps the burst
# alive. `_retry_phase_jitter` offsets the whole schedule by a stable
# per-agent amount, so retry waves stay de-phased across the fleet.
# LangGraph's own `jitter=True` adds only uniform(0, 1)s — a rounding error
# at these scales — which is why the per-agent offset exists.
_RETRY_JITTER_SPAN_S = 10.0
_RETRY_REMAINING_ATTR = "_ava_retry_budget_remaining_seconds"
_retry_budget_state = threading.local()


def _retry_wait_ceiling() -> float | None:
    """The current failed attempt's remaining retry budget, if the node supplied one.

    LangGraph calls ``retry_on`` and then reads the policy fields synchronously
    before awaiting its sleep. A thread-local handoff keeps a hosted runner's
    concurrent turn tasks independent without adding a contextvar mechanism.
    """
    remaining = getattr(_retry_budget_state, "remaining_seconds", None)
    return remaining if isinstance(remaining, float) else None


def _retry_phase_jitter() -> float:
    """Deterministic per-agent offset in [0, _RETRY_JITTER_SPAN_S); 0 when no agent id.

    Same de-phasing idea as services/heartbeat/daemon.py's per-agent
    due-time jitter: correlated failures hit the whole fleet at once, so an
    identical retry schedule makes every agent retry at the same instants.
    Offsetting the schedule start by a stable per-agent amount (derived from
    the bound turn identity) spreads the retry waves; the
    offset is deterministic so an agent keeps its own phase across restarts.
    Absent an identity (tests, non-agent entry points) → 0 (no offset).
    """
    from shared.turn_identity import effective_agent_id

    ident = effective_agent_id()
    if ident is None:
        return 0.0
    return _RETRY_JITTER_SPAN_S * (ident % 1000) / 1000.0


class _TurnScopedRetryPolicy(RetryPolicy):
    """A `RetryPolicy` whose two per-agent fields resolve when they are READ.

    The agent host builds ONE graph for every local
    agent — it has to, because `build_graph` mutates process-global plugin
    registration — so a baked value would give every hosted agent whichever
    agent's context happened to be current at daemon boot. That silently costs
    the two things this policy exists to make per-agent:

    - `max_attempts`, resolved per MODEL, so an agent whose overlay pins a
      different model gets that model's cap;
    - the `_retry_phase_jitter()` term in `initial_interval`, whose only job is
      de-phasing fleet-wide retry waves. Collapsed to one shared value, a
      correlated 429 burst makes every agent retry at the same instant and
      re-synchronises the burst — the exact failure the offset was added to
      prevent (LangGraph's own `jitter=True` adds only uniform(0,1)s, a rounding
      error at a 30s initial interval).

    Both properties read the turn contextvars, so under the host's per-turn bind
    they resolve to the running agent's values. Outside a turn they read the
    cluster defaults.

    ## Why this works, and what would break it

    LangGraph reads every policy field by ATTRIBUTE, at retry time, never by
    unpacking or copying the tuple: `pregel/_retry.py` (sync 660-672, async
    816-828) reads `.max_attempts` / `.initial_interval` / `.max_interval` /
    `.backoff_factor` / `.jitter`, and `_should_retry_on` reads `.retry_on`.
    `graph/state.py` passes the object through by reference, and
    `pregel/_read.py:173` wraps a lone policy as `(policy,)` behind an
    `isinstance(..., RetryPolicy)` check that a subclass satisfies — so it is
    never iterated field-wise.

    That is a dependency's internal read timing, so it is pinned by
    `tests/agent/test_turn_scoped_retry.py`, which drives LangGraph's real retry
    loop rather than asserting on this class alone. If a future version snapshots
    the policy instead, that test fails loudly — and the constructor below still
    fills the underlying tuple slots with the build-time values, so even an
    unnoticed snapshot degrades to today's behaviour rather than to LangGraph's
    defaults.
    """

    __slots__ = ()

    @property
    def max_attempts(self) -> int:  # pyright: ignore[reportIncompatibleVariableOverride]
        from shared.lm.registry import resolve_setting

        return resolve_setting("llm_retry_max_attempts", model=turn_settings.lm.llm_model)

    @property
    def initial_interval(self) -> float:  # pyright: ignore[reportIncompatibleVariableOverride]
        return settings.lm.llm_retry_initial_interval_seconds + _retry_phase_jitter()

    @property
    def max_interval(self) -> float:  # pyright: ignore[reportIncompatibleVariableOverride]
        remaining = _retry_wait_ceiling()
        if remaining is None:
            return settings.lm.llm_retry_max_interval_seconds
        # LangGraph adds up to one second of jitter after this cap. Reserve it
        # so a retry sleep cannot drift beyond the total node budget.
        reserved_jitter = 1.0 if remaining > 1.0 else 0.0
        return min(settings.lm.llm_retry_max_interval_seconds, remaining - reserved_jitter)

    @property
    def jitter(self) -> bool:  # pyright: ignore[reportIncompatibleVariableOverride]
        remaining = _retry_wait_ceiling()
        # LangGraph reads max_interval before jitter. Consume the synchronous
        # handoff here so a later unrelated policy inspection cannot reuse an
        # earlier attempt's budget.
        _retry_budget_state.remaining_seconds = None
        return remaining is None or remaining > 1.0


def _build_llm_retry() -> RetryPolicy:
    """LLM node retry policy — reads params from settings, covers DeepSeek streaming drift.

    Excludes fatal stream/provider errors from retry. The node attaches its
    remaining wall-clock retry budget to retryable exceptions; when the budget
    reaches zero, this predicate rejects the next retry before LangGraph sleeps
    or invokes the provider again.

    Returns a `_TurnScopedRetryPolicy`: the two per-agent fields resolve per read
    so one shared graph still retries each hosted agent on its own schedule.
    """
    from agent.graph._llm import (
        FatalLLMStreamError,
        FatalProviderError,
    )

    def _should_retry(exc: Exception) -> bool:
        # asyncio.CancelledError is a BaseException subclass (not Exception),
        # so it won't reach this callable. All other exceptions: retry.
        if isinstance(
            exc, (FatalLLMStreamError, FatalProviderError, KeyboardInterrupt, SystemExit)
        ):
            _retry_budget_state.remaining_seconds = None
            return False
        remaining = getattr(exc, _RETRY_REMAINING_ATTR, None)
        if isinstance(remaining, float):
            if remaining <= 0.0:
                _retry_budget_state.remaining_seconds = None
                return False
            _retry_budget_state.remaining_seconds = remaining
        else:
            _retry_budget_state.remaining_seconds = None
        return True

    from shared.lm.registry import resolve_setting

    return _TurnScopedRetryPolicy(
        # These two are shadowed by the properties above for every attribute
        # read; they fill the underlying tuple slots so that a consumer which
        # ever reads the policy POSITIONALLY sees this graph's build-time values
        # — today's behaviour — rather than LangGraph's own defaults.
        # Per-model default with shared fallback; an explicit
        # AVA_LLM_RETRY_MAX_ATTEMPTS / per-agent overlay wins.
        max_attempts=resolve_setting("llm_retry_max_attempts", model=turn_settings.lm.llm_model),
        # + _retry_phase_jitter(): per-agent schedule offset (see module note).
        initial_interval=settings.lm.llm_retry_initial_interval_seconds + _retry_phase_jitter(),
        backoff_factor=2,
        max_interval=settings.lm.llm_retry_max_interval_seconds,
        # Explicit: lock LangGraph's per-attempt jitter on (default True, but
        # the intent is load-bearing — see the de-phasing note above).
        jitter=True,
        retry_on=_should_retry,
    )


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


def _register_plugin_parent_packages(pkg: str, name: str, plugin_dir: Path) -> None:
    """Make `pkg.<name>` importable as a namespace-package chain, so relative
    imports inside plugin.py resolve regardless of sys.path / cwd.

    External plugins load under the dotted prefix ``plugins.<name>``, and
    resolving that prefix through the normal import machinery requires
    ``$AVA_HOME`` to be on sys.path. It is not in the exec child (``python -I
    -m agent.exec_child`` boots with cwd=$AVA_HOME/source): ``import plugins``
    there resolves to the checkout's own legacy ``plugins/`` directory when one
    exists, or to nothing — either way ``from . import _sibling`` inside
    plugin.py raises ModuleNotFoundError and took ``import ava`` down with it
    (2026-08-28 ava_ledger incident). The framework owns the dotted prefix, so
    it registers the parent packages itself: ``pkg`` as a namespace package
    over the external plugins root and ``pkg.<name>`` over this plugin's
    directory. Existing sys.modules entries are left untouched — the
    ``pkg.<name>`` registration is what resolves relative imports, the
    top-level one covers a fresh ``import plugins.X`` elsewhere.
    """
    import types

    if sys.modules.get(pkg) is None:
        parent = types.ModuleType(pkg)
        parent.__path__ = [str(plugin_dir.parent)]  # pyright: ignore[reportAttributeAccessIssue]
        sys.modules[pkg] = parent
    child_name = f"{pkg}.{name}"
    if sys.modules.get(child_name) is None:
        child = types.ModuleType(child_name)
        child.__path__ = [str(plugin_dir)]  # pyright: ignore[reportAttributeAccessIssue]
        sys.modules[child_name] = child


def _report_plugin_load_failure(name: str, exc: BaseException) -> None:
    """The loud half of the fail-soft contract: a loguru ERROR carrying the
    traceback plus one ``plugin_load_failed`` telemetry event, so ops sees
    which plugin broke and why. Never raises — the failure already happened."""
    from shared.log import logger
    from shared.telemetry import emit

    logger.error(
        "[plugins] plugin {} failed to load — skipped (fail-soft); "
        "the remaining plugins still load",
        name,
        exc_info=exc,
    )
    with contextlib.suppress(Exception):
        emit(
            "telemetry",
            "plugin_load_failed",
            level="error",
            attributes={"plugin": name, "error": f"{type(exc).__name__}: {exc}"},
        )


def _load_extensions() -> plugins_cfg.PluginsConfig:
    """Read plugins.json, trigger import side-effects for enabled plugins (hook
    registration + Layer A wrap + system prompt contribution).

    Unified plugin model (2026-05 refactor):
    - Built-in plugins in `<repo>/ava_builtins/plugins/<name>/plugin.py`
    - External plugins in `~/.ava/plugins/<name>/plugin.py`
    - Name collision → DuplicatePlugin fail-fast
    - plugins.json `plugins` section controls each plugin's enabled/disabled

    Flow:
    0. Clear ghost state accumulated from previous reload (state field / hook / contributor)
    1. plugins_cfg._discover_plugins() → scan built-in + external dirs, returns {name: path}
    2. plugins_cfg.load(known_plugins) → read config + auto-merge new plugins
    3. For each enabled=true plugin: import plugin.py to trigger side-effects
       (reload semantics — a module object already registered for that file is
       re-executed, never replaced)
    """
    # 0. Reset previously registered plugin state / hook / contributor — multiple calls
    # to _load_extensions (test fixture, dev hot-reload) accumulate module-level
    # globals; without reset, "plugins disabled by new config" leave residual hooks
    # in dispatch.
    from agent.state import clear_plugin_registrations

    clear_plugin_registrations()

    # 1. Discover all plugins
    discovered = plugins_cfg._discover_plugins()
    known_plugins = set(discovered.keys())

    # 2. Read config
    try:
        config = plugins_cfg.load(known_plugins)
    except plugins_cfg.DanglingPlugin as exc:
        # Same fail-soft contract as a broken plugin.py: a config entry whose
        # plugin directory is gone (an interrupted upgrade, a manual rm) must
        # not block import ava. Report each dangling name, then reload with
        # the entries dropped (treated as disabled).
        for name in sorted(exc.names):
            _report_plugin_load_failure(name, exc)
        config = plugins_cfg.load(known_plugins, allow_dangling=True)

    # 3. Import enabled plugin (with PluginContext for state key prefixing)
    import importlib.util
    import os.path
    import sys

    from shared.plugin_context import PluginContext

    for name, entry in config.plugins.items():
        if not entry.enabled:
            continue
        # Invariant: load() already validated DanglingPlugin, name must be in discovered;
        # spec_from_file_location returns non-None for an existing .py. Assert rather
        # than silent continue — an enabled plugin silently vanishing with 0 log is
        # the hardest bug to chase.
        assert name in discovered, f"load() invariant broken: {name} not in discovered"  # noqa: S101
        plugin_dir = discovered[name]
        plugin_py = plugin_dir / "plugin.py"
        with PluginContext(name):
            # Use dotted name so importlib auto-sets __package__,
            # which makes `from . import xxx` relative imports in plugin.py work.
            # Built-in plugins live under ava_builtins/plugins/; external under ~/.ava/plugins/.
            is_builtin = str(paths.repo_plugins_dir()) in str(plugin_dir.resolve())
            pkg = "ava_builtins.plugins" if is_builtin else "plugins"
            spec = importlib.util.spec_from_file_location(f"{pkg}.{name}.plugin", plugin_py)
            assert spec is not None and spec.loader is not None, (  # noqa: S101
                f"spec_from_file_location returned None for existing {plugin_py}"
            )
            # Reload, not replace (issue #147). When this dotted name already
            # names *this* file, execute into the module object that is already
            # registered — `importlib.reload` semantics — instead of binding a
            # fresh one. Replacing it forks the module identity: whoever imported
            # the plugin before the load (a test's module-level import, a hook
            # bound at import time) keeps the old object, while every later
            # `sys.modules` lookup — `mock.patch`, `getattr` on the dotted path —
            # resolves the new one, so a patch silently never reaches the code
            # under test. Re-executing keeps one `__dict__`, so both sides see
            # the same globals. A *different* file claiming the same dotted name
            # (synthetic plugins under a tmp dir in tests) is a different module
            # and must not inherit the previous one's globals — bind fresh.
            existing = sys.modules.get(spec.name)
            recorded = getattr(existing, "__file__", None)
            module = (
                existing
                if recorded is not None
                and os.path.realpath(recorded) == os.path.realpath(plugin_py)
                else importlib.util.module_from_spec(spec)
            )
            # Register to sys.modules **before exec_module** — standard importlib idiom:
            # BaseModel class definitions inside the plugin trigger Pydantic's
            # `__init_subclass__`, which calls `get_type_hints` to resolve
            # Annotated[T, reducer] string annotations (under
            # `from __future__ import annotations` annotations are ForwardRef);
            # get_type_hints walks `sys.modules[cls.__module__].__dict__` to get
            # globals for eval. Without registering sys.modules → ForwardRef
            # cannot be resolved, model_fields.annotation retains ForwardRef,
            # later LangGraph `StateGraph(schema)` get_type_hints eval again
            # raises NameError. After registering, globals are reachable, fields
            # resolve directly into real types.
            sys.modules[spec.name] = module
            try:
                # External plugins only: builtins resolve through the real
                # `ava_builtins.plugins` package (source is on sys.path), and
                # shadowing it with a synthetic module would hide whatever its
                # __init__.py defines from later `importlib.import_module`
                # callers (e.g. gateway/routers/_plugin_metrics.py).
                if pkg == "plugins":
                    _register_plugin_parent_packages(pkg, name, plugin_dir)
                spec.loader.exec_module(module)
            except KeyboardInterrupt:
                sys.modules.pop(spec.name, None)
                raise
            except SystemExit:
                sys.modules.pop(spec.name, None)
                raise
            except BaseException as exc:
                # Fail-soft contract (2026-08-28 ava_ledger incident): a broken
                # plugin — a missing sibling module, a syntax error, a top-level
                # exception — degrades to a skip with a loud warning, never a
                # blocked `import ava` / graph build for the whole cluster. The
                # remaining enabled plugins keep loading below. The half-executed
                # module is dropped from sys.modules so a later reload retries
                # from a clean slate.
                sys.modules.pop(spec.name, None)
                _report_plugin_load_failure(name, exc)

    # 4. Bind plugin config from disk — only batch bind after all plugin imports complete,
    # so that when hook callbacks actually fire, `ava._settings.plugins.<n>` is ready.
    # Missing disk image auto-writes default; schema drift raises (guides
    # `ava plugins update`).
    from shared.plugin_config_registry import bind_from_disk

    bind_from_disk()

    # 5. Install the SDK-usage recorder over the final ava.* surface. Runs last so it
    # wraps plugin-registered namespaces / members and sits outermost of any plugin
    # `ava.extend.wrap` layer (one count per agent call). Idempotent; a plugin reload
    # re-runs it after clear_wraps restores plugin-touched targets.
    from agent import sdk_metering

    sdk_metering.install()

    return config


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
    `_node_log.node_lifecycle` before yield (includes msg_count =
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
    _load_extensions()

    # Register built-in hooks. Must run after _load_extensions() because
    # clear_plugin_registrations() (called at the top of _load_extensions)
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
    g.add_node(LLM, llm_node, retry_policy=_build_llm_retry())  # type: ignore[arg-type]
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
