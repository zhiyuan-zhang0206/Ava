"""build_graph: assemble 8-Node self-cycling topology, all Command(goto=) routing.

Deps (ops_pool / llm / event_publisher) are injected via `Runtime[AvaContext]` —
build_graph does not take them; the caller passes them via
`graph.ainvoke(..., context=AvaContext(...))`. Node functions access them via
`runtime.context.X`.

At startup, `_load_extensions()` reads `$AVA_HOME/plugins_config.json` and
imports the `plugin.py` of every enabled plugin, by path — builtin and external
alike, one loop — followed by each plugin's `agent_runtime.py` face (state
fields, hooks, prompt sections). The loader lives in `agent._extensions` (task
#3633 moved it off this module so surface-only processes never need the graph
kernel); it is re-exported here as `_load_extensions` for the graph build. The
import mechanics and the fail-soft contract live in `ava._extend`
(`load_plugin_module` / `safe_load_plugin_module`), the same primitives
`ava._extend.scan_and_load` uses at host boot, so both production load paths
agree on module name, package context, `sys.modules` identity, and containment.
A repeat call re-executes the module already in `sys.modules` rather than
binding a new one, so a plugin module's identity is stable for the life of the
process. Layer A wrap monkey-patches the process's ava module; the exec child
re-runs the plugin surface load at its own boot, so agent code there sees the
wrapped version too. Config decides what is imported; not imported = not
registered.
"""

import threading

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import RetryPolicy

from agent._extensions import load_extensions as _load_extensions
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
from shared.config import settings
from shared.config.turn_view import turn_settings
from shared.context import AvaContext

from ._claim import claim_node
from ._exec import exec_node
from ._init_context import init_context_node
from ._llm import llm_node

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
# The backoff factor the transient schedule was tuned with (all incidents
# validated base 2) — the `backoff_factor` property below returns this unless
# the delayed stall mode computes its sleep whole in `_should_retry`.
_LLM_RETRY_BACKOFF_FACTOR = 2.0
_retry_budget_state = threading.local()


def _retry_thread_id() -> str:
    """The per-thread stall-pair streak key (bound turn identity, else `?`).

    `agent.graph._llm_errors` keys its streaks by the llm node's
    ``str(agent_id_from_config(config))``; the retry machinery runs in the same
    turn, so the bound identity resolves to the same string. `?` keeps tests
    and non-agent entry points from crashing on an unbound identity.
    """
    from shared.turn_identity import effective_agent_id

    ident = effective_agent_id()
    return str(ident) if ident is not None else "?"


def _delayed_stall_sleep(streak: int) -> float:
    """The jittered wait before the retry after stall pair number `streak`.

    Exponential with a cap: ``initial x 2**(streak-1)``, capped at
    ``llm_stall_retry_max_interval_seconds``. Then multiplicative
    ±``llm_stall_retry_jitter_fraction`` jitter — the 2026-09-14/15 wave hit 36
    agents on 3 machines within 10s, so an unjittered schedule would
    re-synchronize the fleet into a fresh burst. Random per grant (not the
    deterministic per-agent phase used elsewhere): with only up to 4 grants,
    each one must land in a fresh spot, not offset a fixed schedule.
    """
    base = min(
        settings.lm.llm_stall_retry_initial_interval_seconds * (2 ** (streak - 1)),
        settings.lm.llm_stall_retry_max_interval_seconds,
    )
    from shared.resilience import jittered

    return jittered(base, span=base * settings.lm.llm_stall_retry_jitter_fraction, mode="random")


def _retry_wait_ceiling() -> float | None:
    """The current failed attempt's remaining retry budget, if the node supplied one.

    LangGraph calls ``retry_on`` and then reads the policy fields synchronously
    before awaiting its sleep. A thread-local handoff keeps a hosted runner's
    concurrent turn tasks independent without adding a contextvar mechanism.
    """
    remaining = getattr(_retry_budget_state, "remaining_seconds", None)
    return remaining if isinstance(remaining, float) else None


def _delayed_stall_sleep_pending_value() -> float | None:
    """The delayed stall wait `_should_retry` stashed for this attempt, if any.

    Same synchronous handoff shape as `_retry_wait_ceiling`: LangGraph reads
    the policy fields one after another before its sleep, so a thread-local
    carries the computed wait from the predicate to the property reads. It is
    consumed (cleared) by the `jitter` read, the last field LangGraph touches.
    """
    value = getattr(_retry_budget_state, "stall_pair_sleep", None)
    return value if isinstance(value, float) else None


def _delayed_stall_sleep_pending() -> bool:
    """True while this attempt's reads are serving a delayed stall wait."""
    return _delayed_stall_sleep_pending_value() is not None


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

    A third dynamic behaviour rides the same handoff: the delayed stall
    schedule. `_should_retry`'s pair branch computes the whole wait for this
    attempt (streak-based, jittered — `_delayed_stall_sleep`) and stashes it in
    the thread-local; `initial_interval` / `max_interval` then serve it,
    `backoff_factor` collapses to 1.0 (the wait is not compounded again),
    `jitter` turns off (the wait already carries its multiplicative jitter) and
    `max_attempts` adds the streak cap as headroom. The stash is consumed by
    the `jitter` read, exactly like the transient budget above.
    """

    __slots__ = ()

    @property
    def max_attempts(self) -> int:  # pyright: ignore[reportIncompatibleVariableOverride]
        from shared.lm.registry import resolve_setting

        base = resolve_setting("llm_retry_max_attempts", model=turn_settings.lm.llm_model)
        if _delayed_stall_sleep_pending():
            # Non-binding headroom while a delayed stall sequence runs: its
            # real bounds are the streak cap (in retry_on) and the node-entry
            # fatal, but the shared attempts gate must not pre-empt them when
            # transient failures earlier in the same sequence consumed part of
            # the transient count.
            return base + settings.lm.llm_stall_retry_max_consecutive
        return base

    @property
    def initial_interval(self) -> float:  # pyright: ignore[reportIncompatibleVariableOverride]
        delayed = _delayed_stall_sleep_pending_value()
        if delayed is not None:
            return delayed
        return settings.lm.llm_retry_initial_interval_seconds + _retry_phase_jitter()

    @property
    def backoff_factor(self) -> float:  # pyright: ignore[reportIncompatibleVariableOverride]
        # The delayed stall wait is computed whole in `_should_retry`
        # (streak-based, jittered); compounding it again by the transient
        # schedule's factor of 2 would square the growth.
        return _LLM_RETRY_BACKOFF_FACTOR if not _delayed_stall_sleep_pending() else 1.0

    @property
    def max_interval(self) -> float:  # pyright: ignore[reportIncompatibleVariableOverride]
        delayed = _delayed_stall_sleep_pending_value()
        if delayed is not None:
            return delayed
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
        delayed = _delayed_stall_sleep_pending_value()
        # LangGraph reads max_interval before jitter. Consume the synchronous
        # handoffs here so a later unrelated policy inspection cannot reuse an
        # earlier attempt's budget or delayed wait.
        _retry_budget_state.remaining_seconds = None
        _retry_budget_state.stall_pair_sleep = None
        if delayed is not None:
            # The delayed wait was already jittered multiplicatively in
            # `_should_retry`; LangGraph's additive +0..1s would only blur the
            # configured ±fraction band.
            return False
        return remaining is None or remaining > 1.0


def _build_llm_retry() -> RetryPolicy:
    """LLM node retry policy — reads params from settings, covers DeepSeek streaming drift.

    Excludes fatal stream/provider errors from retry. The node attaches its
    remaining wall-clock retry budget to retryable exceptions; when the budget
    reaches zero, this predicate rejects the next retry before LangGraph sleeps
    or invokes the provider again.

    Stall pairs (`LLMStreamStallPairError` — the streaming segment stalled and
    the non-streaming fallback then timed out) run on a SEPARATE delayed
    schedule instead of the transient one: a manager/provider that was just
    unable to serve two consecutive segments needs minutes, not the 30s
    transient backoff, so the waits start at
    `llm_stall_retry_initial_interval_seconds` (5min), double, cap at
    `llm_stall_retry_max_interval_seconds` (30min), and run for at most
    `llm_stall_retry_max_consecutive` (4) consecutive pairs before the node's
    entry check fails the turn into the fatal settlement (agent alive, idles;
    the regular wake path retries). Each wait is jittered ±
    `llm_stall_retry_jitter_fraction` so the fleet's delayed retries do not
    re-synchronize (the 2026-09-14/15 wave hit 36 agents within 10s).

    Returns a `_TurnScopedRetryPolicy`: the two per-agent fields resolve per read
    so one shared graph still retries each hosted agent on its own schedule.
    """
    from agent.graph._llm_errors import (
        FatalLLMStreamError,
        FatalProviderError,
        LLMStreamStallPairError,
        _record_stall_pair_streak,
        _reset_stall_pair_streak,
        _stall_pair_streak,
    )

    def _should_retry(exc: Exception) -> bool:
        # asyncio.CancelledError is a BaseException subclass (not Exception),
        # so it won't reach this callable. All other exceptions: retry.
        if isinstance(
            exc, (FatalLLMStreamError, FatalProviderError, KeyboardInterrupt, SystemExit)
        ):
            _retry_budget_state.remaining_seconds = None
            _retry_budget_state.stall_pair_sleep = None
            return False
        max_pairs = settings.lm.llm_stall_retry_max_consecutive
        if isinstance(exc, LLMStreamStallPairError) and max_pairs > 0:
            # Delayed stall schedule: minutes-scale waits between attempts, at
            # most `max_pairs` consecutive pairs (a pair = stream segment
            # stall + non-streaming fallback stall in the same call). This is
            # a regime of its own on top of the transient budget's
            # seconds-scale backoff; a spent streak ends the sequence at the
            # node-entry check, so the refusal here is only the defensive
            # backstop.
            thread = _retry_thread_id()
            streak = _stall_pair_streak(thread) + 1
            if streak > max_pairs:
                _reset_stall_pair_streak(thread)
                _retry_budget_state.remaining_seconds = None
                _retry_budget_state.stall_pair_sleep = None
                return False
            _record_stall_pair_streak(thread, streak)
            _retry_budget_state.remaining_seconds = None
            _retry_budget_state.stall_pair_sleep = _delayed_stall_sleep(streak)
            return True
        remaining = getattr(exc, _RETRY_REMAINING_ATTR, None)
        if isinstance(remaining, float):
            if remaining <= 0.0:
                _retry_budget_state.remaining_seconds = None
                _retry_budget_state.stall_pair_sleep = None
                return False
            _retry_budget_state.remaining_seconds = remaining
        else:
            _retry_budget_state.remaining_seconds = None
        _retry_budget_state.stall_pair_sleep = None
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
        backoff_factor=_LLM_RETRY_BACKOFF_FACTOR,
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
