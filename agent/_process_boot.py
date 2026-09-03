"""Agent process boot — the one-shot startup phases.

Everything between `python -m agent` and the graph going live, split out of
`agent/loop.py` (which keeps `main()` orchestration + the `run()` entry):

- framework-scope per-agent config (birth_config first, config_overlay on top)
  + the sdk_disable delta, applied BEFORE `build_chat_model` so
  `turn_settings.lm.llm_model` reflects this agent's model;
- boot phase 1 (`_boot_agent_process`): process init, MCP daemon handle, trace
  export init, ava SDK identity (`ava._boot.establish`), plugin load, workspace
  pre-create, model build — composed from the two SCOPES below;
- boot phase 2 (`_build_data_plane`): the inbound Redis pub/sub listener + the
  SSE event publisher;
- `_build_checkpointer`: the AsyncPostgresSaver (wrapped with loud-failure
  logging and optional N-step checkpoint throttling) + the startup
  claimed-inbound reconcile;
- `_build_graph`: the compiled graph + dangling tool pairing repair + the
  plugin-scope config overlay + the effective-config snapshot.

The MCP daemon is a per-machine SHARED cluster service (ops roster session
"mcp-daemon", watchdog-managed; socket $AVA_HOME/run/mcp_daemon.sock carries
no agent_id). The handle `_boot_agent_process` returns is a no-op kept for
boot-path compatibility: spawn()/await_ready() return immediately — the shared
daemon is supervised independently and is already listening before any agent
boots, so there is no per-agent fork or socket-bind cost here.

Framework-scope config must apply BEFORE build_chat_model so
`turn_settings.lm.llm_model` reflects it if this agent runs a different model.
Plugin-scope is deferred until after build_graph's bind_from_disk has
populated _PLUGIN_CONFIGS — see the second apply_config_overlay call in
`_build_graph`.

## The two boot scopes

`_boot_agent_process` is the one-process-per-agent composition of two halves
that are NOT the same scope, and the hosted agent-runner
(`future/infra/agent-runner-as-server.md`, `services/agent_host/`) needs them
apart: it serves many agents from one process, so it runs the process half once
at daemon boot and the agent half per agent.

- `init_process_scope` / `land_cluster_extensions` / `load_process_extensions` —
  **process** scope: OTLP trace export init, the cluster-owned skill
  materialization, and the external-plugin load. The first and third mutate
  process globals (the tracer provider; `sys.modules` + the plugin registries),
  so "once" is a correctness requirement in the hosted runner, not a saving —
  see each function's docstring. The middle one is process scope for a different
  reason: it writes the machine's skills directory, which every agent on the box
  shares, so per-agent repetition would be pure cost.
- `boot_agent_scope` — **agent** scope: the workspace pre-create, the desktop
  permissions notice, and the chat model, which is built from
  `turn_settings.lm.llm_model` and therefore differs per agent whenever an
  overlay pins a model.

Splitting them moved `workspace_dir` from just before the plugin load to just
after it (the agent half must follow the process half, because
`_notify_desktop_permissions_at_startup` needs the SDK loaded). It is an idempotent
mkdir and no plugin touches the workspace at import time, so the two orders are
equivalent.
"""

import os
from pathlib import Path
from typing import cast

import psycopg
import redis.asyncio as aredis
from langchain_core.language_models.chat_models import BaseChatModel
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph.state import CompiledStateGraph
from psycopg.rows import DictRow
from psycopg_pool import AsyncConnectionPool

import ava
import ava._boot
from shared.config import settings
from shared.config.turn_view import turn_settings
from shared.event_publisher import AgentEventPublisher
from shared.lm.factory import build_chat_model
from shared.log import init_agent_process, logger
from shared.paths import workspace_dir
from shared.redis_client import get_async_redis
from shared.redis_listener import RedisInboundListener

from . import _boot_timing
from .graph import build_graph
from .graph._context import AvaContext
from .mcp_daemon import _MCPDaemon
from .startup import (
    _notify_desktop_permissions_at_startup,
    _reconcile_claimed_inbounds_at_startup,
    _repair_dangling_tool_use_at_startup,
    _wrap_saver_writes_with_loud_failure,
    _wrap_saver_writes_with_nstep_interval,
    _write_effective_config_to_restart_completed,
)
from .state import BaseAgentState, build_checkpoint_serde


def _apply_per_agent_sdk_disable() -> None:
    """Apply per-agent sdk_disable delta after config_overlay framework scope.

    sdk_disable is applied at ``ava`` import time from the env var
    ``AVA_SDK_DISABLE``.  Per-agent overlay additions to sdk_disable are
    set on settings by ``apply_config_overlay`` and must be applied on top
    of the env baseline — ``ava._apply_sdk_disable`` is idempotent, so
    only genuinely new entries take effect.
    """

    if not turn_settings.agent.sdk_disable:
        return
    env_entries = set(ava._sdk_disable_entries)
    new_disable = [e for e in turn_settings.agent.sdk_disable if e not in env_entries]
    if new_disable:
        ava._apply_sdk_disable(new_disable)


def _apply_per_agent_eval_isolation() -> None:
    """Apply the SDK and memory-pool boundaries for an isolated eval agent.

    This runs after plugins have registered their namespaces: the eval boundary
    must rebind the live `ava.memory` surface rather than affect the plugin's
    import-time default path.
    """
    if not turn_settings.agent.eval_isolation:
        return

    allowed_network = set(turn_settings.agent.eval_network_allowlist)
    disabled = ["agents.get_last_message", "tasks", "mcps", "ui"]
    if "web" not in allowed_network:
        disabled.append("web")
    if "understand" not in allowed_network:
        disabled.append("understand")
    ava._apply_sdk_disable(disabled)

    agent_id = int(os.environ["AVA_AGENT_ID"])
    isolated_pool = workspace_dir(agent_id) / "memory-pool"
    isolated_pool.mkdir(parents=True, exist_ok=True)
    memory = getattr(ava, "memory", None)
    if memory is not None:
        memory.PATH = ava.const(isolated_pool, doc=memory.PATH.__doc__)
        memory.search = _isolated_memory_search
        memory.search_detailed = _isolated_memory_search


def _isolated_memory_search(_query: str, _k: int = 5) -> list[tuple[Path, str, list[str]]]:
    """Return no shared-memory results for an isolated evaluation agent."""
    return []


def _apply_per_agent_framework_config(
    config_overlay: dict[str, object] | None, birth_config: dict[str, object] | None
) -> None:
    """Apply this agent's two stored config maps onto the settings singleton.

    birth_config first, config_overlay on top: both write the same singleton via
    `set_field`, so the last writer wins and the resulting precedence is
    `config_overlay > birth_config > current config` — a field named in neither map
    is simply left at whatever the live config already resolved to.

    birth_config holds only framework fields (the frozen set is framework-only), so
    unlike the overlay it has no deferred plugin-scope half.
    """
    if not (birth_config or config_overlay):
        return
    from shared.plugin_config_registry import apply_config_overlay

    if birth_config:
        apply_config_overlay(birth_config, scope="framework")
    if config_overlay:
        apply_config_overlay(config_overlay, scope="framework")
    _apply_per_agent_sdk_disable()


def init_process_scope() -> None:
    """Process-scope boot: start OTLP trace init.

    Only the cheap decisions (disk guards + collector preflight) run here;
    the heavy traceloop import + init proceeds on a daemon thread, so the boot
    path stays sub-second. OpenLLMetry must still be installed before the
    first turn (the LangChain callback-manager wrap and the SDK instrumentors
    are call-time, and the turn root span needs the provider set) — that
    ordering is enforced by `shared.trace.ensure_init_resolved` inside
    `turn_span`, which is what the first graph invocation waits on.

    Process scope, not agent scope: `initialize_tracing` installs the global
    tracer provider, and the span attribution that distinguishes agents is the
    per-turn root span (`shared.trace.turn_span`), not the provider. The hosted
    runner calls this once at daemon boot.
    """
    from shared.trace import initialize_tracing

    initialize_tracing()


def land_cluster_extensions() -> None:
    """Process-scope boot: land the cluster's installed skills onto this machine.

    The boot-side sibling of `cli/commands/_converge_extensions.py`
    (`materialize_cluster_extensions`), over the same
    `shared.extension_materialize.materialize_skills`. Converge covers the
    operator path — `ava start`, `ava converge`; this covers the one that needs
    no operator at all, which is what closes the offline window: a machine that
    was down when someone ran `ava skill install` elsewhere catches up the moment
    anything on it next starts, and an agent never boots against a tree older
    than the registry row it could have read
    (`future/infra/extension-ownership.md` S2).

    Process scope, not agent scope: the skills directory is a fact about the
    MACHINE, identical for every agent on it. The hosted runner therefore calls
    this once at daemon boot, beside the other two process-scope halves, rather
    than per agent.

    Runs before `load_process_extensions` so the ordering stays correct when
    plugins become registry-owned in a later slice. Today it does not matter —
    skills are read per turn and plugins still come from the checkout — which is
    exactly why it is worth fixing now rather than after the ordering has a
    consequence.

    Failures are logged, not raised, and that is a different judgement from the
    one boot usually makes. Boot fails fast on the things an agent cannot work
    without; a stale skills directory is not one of them, the registry retries on
    the next start, and refusing to boot over it would convert a recoverable lag
    into an outage. Same stance as converge, and the opposite of the install
    path's, which is where the fact is CREATED.

    On a cluster with no installed extensions this is one indexed query
    returning no rows.
    """
    from shared import db, extension_materialize, paths

    try:
        with db.connect() as conn:
            result = extension_materialize.materialize_skills(conn, dest_root=paths.skills_dir())
    except Exception as exc:
        logger.warning(
            "[extensions] could not read the cluster registry at boot ({}); this "
            "machine keeps whatever skills it already has and retries on the next start",
            exc,
        )
        return
    if result.changed:
        logger.info(
            "[extensions] landed {} skill(s), updated {}",
            len(result.landed),
            len(result.updated),
        )


def load_process_extensions() -> None:
    """Process-scope boot: import every external plugin under `$AVA_HOME/plugins`.

    Import side effects are the registration (hooks, Layer A wraps, system-prompt
    contributions), and they must land before the first exec node runs agent code
    — plugins may monkey-patch `ava.X.y`.

    **Exactly once per process.** Repeating it is not a supported way to pick up
    a newly installed plugin: plugin-spec-v2's S4 dispose contract is
    unimplemented, so a second load leaks whatever the first allocated and forks
    class identity for anything that captured a plugin class before it (PR #154
    made the module object stable, which removes a different obstacle, not this
    one). The consequence is visible in hosted mode — process mode reloads on
    each agent spawn, hosted mode only on a runner restart — and is a known,
    filed behavioural difference, not something to work around here.
    """
    ava._extend.scan_and_load()


async def boot_agent_scope(agent_id: int) -> BaseChatModel:
    """Agent-scope boot: workspace pre-create, screen-capture notice, chat model.

    Everything here is a fact about ONE agent, so the hosted runner runs it per
    agent (cached, keyed on the agent's stored config) while the process-scope
    half above runs once for the whole daemon.

    The workspace pre-create is unconditional, not gated by
    `settings.agent.workspace_in_system_prompt`: the folder is the relative-path
    base for `ava.files` / `ava.shell.run`, so it must exist even when the prompt
    section advertising it is off (bench runners).

    The chat model is built from `turn_settings.lm.llm_model`, so callers must
    have this agent's framework-scope config in effect first — the singleton
    write in process mode (`_apply_per_agent_framework_config`), the contextvar
    bind in hosted mode (`shared.config.turn_view.bind_agent_config`).

    Building it eagerly is safe even though the trace init is still in flight:
    traceloop's LangChain wrap injects its callback handler into every
    callback manager at CONFIGURATION time (per run / per call), so a model
    constructed before the init completes still produces spans on its first
    call — and that first call happens inside a turn, after turn_span has
    waited for the init.

    Returns:
        This agent's chat model.
    """
    workspace_dir(agent_id)
    # When converge detected an unavailable desktop permission, notify once
    # (idempotent -- clears claimed status files after). Must run after the
    # SDK/plugin load so ava.ui.notify is available.
    await _notify_desktop_permissions_at_startup()
    return build_chat_model(turn_settings.lm.llm_model)


async def _boot_agent_process(
    agent_id: int,
    config_overlay: dict[str, object] | None,
    birth_config: dict[str, object] | None = None,
) -> tuple[_MCPDaemon, BaseChatModel]:
    """Boot phase 1: process init, MCP daemon spawn, SDK identity, plugin
    load, framework-scope config, and the chat model build.

    The MCP daemon is a per-machine SHARED cluster service (ops roster
    session "mcp-daemon", watchdog-managed; socket $AVA_HOME/run/mcp_daemon.sock
    carries no agent_id). This handle is a no-op kept for boot-path
    compatibility: spawn()/await_ready() return immediately — the shared
    daemon is supervised independently and is already listening before any
    agent boots, so there is no per-agent fork or socket-bind cost here.

    Framework-scope config (birth_config first, config_overlay on top) must
    apply BEFORE build_chat_model so `turn_settings.lm.llm_model` reflects it if
    this agent runs a different model. Plugin-scope is deferred until after
    build_graph's bind_from_disk has populated _PLUGIN_CONFIGS — see the
    second apply_config_overlay call in `_build_graph`.
    """
    # claim_agent_row is called early in __main__.py before the heavy import
    # chain. It atomically takes an unclaimed idling row into 'running' before
    # this boot phase proceeds.
    init_agent_process(agent_id=agent_id)

    mcp_daemon = _MCPDaemon(agent_id)
    await mcp_daemon.spawn()

    # ── trace recording init (OTLP export to the local collector sidecar) ──
    init_process_scope()
    _boot_timing.mark("trace_init")

    # ── ava SDK in-process init ──
    # The main process has already `import ava` (top-level) at module load
    # time, with DB/Redis connections and submodules ready; here we establish
    # this process's identity (sets ava.self.AGENT_ID; owns_loop=True marks it
    # as the process that drives the turn loop, so lifecycle self-actions are
    # permitted) and load plugins — must happen before the first exec_node runs
    # agent code (plugins may monkey-patch ava.X.y functions).
    ava._boot.establish(agent_id, owns_loop=True)
    # Forward the agent identity into the environment so child processes inside
    # persistent shell sessions (and watchers) can pick it up.  shared.session_env
    # already forwards every AVA_* var onto sessions, so setting this here
    # is the single source — no per-session plumbing needed.
    os.environ["AVA_AGENT_ID"] = str(agent_id)
    land_cluster_extensions()
    load_process_extensions()

    # ── MCP daemon startup ──
    # Agent code in the exec child reaches the daemon via
    # ava.mcps._get_remote_client, which derives the socket path from
    # ava.self.AGENT_ID (the child re-establishes its identity from the
    # request) and connects iff the socket exists. Readiness is awaited just
    # before the graph goes live.

    # Per-agent config, framework-scope: applied BEFORE build_chat_model so
    # `settings.lm.llm_model` reflects it if this agent runs a different model.
    # Plugin-scope is deferred until after build_graph's bind_from_disk has
    # populated _PLUGIN_CONFIGS — see the second apply_config_overlay call below.
    _apply_per_agent_framework_config(config_overlay, birth_config)

    llm = await boot_agent_scope(agent_id)
    _boot_timing.mark("sdk_mcp_model")
    return mcp_daemon, llm


async def _build_data_plane(agent_id: int) -> tuple[RedisInboundListener, AgentEventPublisher]:
    """Boot phase 2: one Redis client + the inbound pub/sub listener + the SSE
    event publisher (started, best-effort fan-out).

    `inbound_listener` owns a separate long-lived Redis pub/sub subscription
    (needs its own connection for `get_message()`; auto-reconnects). The event
    publisher runs one background worker that drains a queue and publishes
    serially, so node code emits live-view events without ever awaiting Redis
    on the control path (drained + stopped in main's finally).
    """
    redis_client: aredis.Redis = get_async_redis()
    inbound_listener = RedisInboundListener(settings.data_plane.redis_url, agent_id)
    event_publisher = AgentEventPublisher(
        redis_client, settings.data_plane.events_channel, agent_id=agent_id
    )
    await event_publisher.start()
    return inbound_listener, event_publisher


async def _build_checkpointer(
    db_pool: AsyncConnectionPool[psycopg.AsyncConnection], agent_id: int
) -> AsyncPostgresSaver:
    """Saver + reconcile: log every checkpoint write failure, optionally
    throttle graph super-step checkpoints by the turn-scoped N-step interval,
    then resolve any 'claimed' inbounds left by a previous process of this agent.

    LangGraph submits aput / aput_writes into a background executor and never
    propagates its failures; a conn that *dies during* aput goes silent. Wrap
    the two write paths so every checkpoint failure lands a
    checkpoint_write_failed event in events, then re-raise (retry /
    propagation paths inside langgraph remain unchanged).

    Reconcile: read state.messages, build the set of inbound ids whose
    HumanMessage actually landed, then have agent/db.py finalize each
    'claimed' row — done if confirmed in state.messages, else back to pending
    for re-delivery on the next claim cycle. See
    decisions/2026-04-26-inbound-queue.md.
    """
    # Schema creation/versioning is a deployment precondition: fresh install
    # calls PostgresSaver.setup(); later upstream changes must ship as paired Ava
    # migrations so rollback can reverse them, and start verifies their exact
    # applied set. Agent processes dial as least-privilege ava_runner and must
    # never attempt DDL. Any missed invariant fails here on the first saver read.
    saver_pool = cast(AsyncConnectionPool[psycopg.AsyncConnection[DictRow]], db_pool)
    checkpointer = AsyncPostgresSaver(conn=saver_pool, serde=build_checkpoint_serde())
    _wrap_saver_writes_with_loud_failure(checkpointer, agent_id)
    _wrap_saver_writes_with_nstep_interval(checkpointer, turn_settings.agent.checkpoint_interval)
    await _reconcile_claimed_inbounds_at_startup(db_pool, checkpointer, agent_id)
    _boot_timing.mark("db_reconcile")
    return checkpointer


async def _build_graph(
    checkpointer: AsyncPostgresSaver,
    config_overlay: dict[str, object] | None,
    agent_id: int,
) -> CompiledStateGraph[BaseAgentState, AvaContext, BaseAgentState, BaseAgentState]:
    """Build the graph, apply eval isolation, repair dangling tool pairing,
    apply the plugin-scope config overlay, and write the effective-config snapshot.

    A hard cancel (SIGTERM / restart / stop kill -> asyncio.CancelledError)
    can leave a tool_use committed without its paired tool_result, or a
    tool_result committed without its carrying tool_use; repair it before
    ainvoke so resurrect does not loop on provider 400s (agents 167, 5333).

    Plugin-scope overlay runs after build_graph's bind_from_disk has populated
    _PLUGIN_CONFIGS; framework-scope already ran in `_boot_agent_process`
    before build_chat_model. The effective_config snapshot is written after
    both halves, so it reflects the actually effective config.

    Eval isolation runs immediately after build_graph registers plugin
    namespaces. Its framework-scope settings are already resolved by
    `_boot_agent_process`, so it can rebind the live plugin SDK surface here.
    """
    graph = build_graph(checkpointer)
    _apply_per_agent_eval_isolation()
    await _repair_dangling_tool_use_at_startup(graph, agent_id)
    if config_overlay:
        from shared.plugin_config_registry import apply_config_overlay

        apply_config_overlay(config_overlay, scope="plugin")
    _write_effective_config_to_restart_completed(agent_id)
    return graph
