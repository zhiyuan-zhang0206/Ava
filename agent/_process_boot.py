"""Agent process boot — the one-shot startup phases.

Everything between `python -m agent` and the graph going live, split out of
`agent/loop.py` (which keeps `main()` orchestration + the `run()` entry):

- framework-scope per-agent config (birth_config first, config_overlay on top)
  + the sdk_disable delta, applied BEFORE `build_chat_model` so
  `turn_settings.lm.llm_model` reflects this agent's model;
- boot phase 1 (`_boot_agent_process`): process init, MCP daemon handle, trace
  export init, ava SDK identity (`ava._boot.establish`), plugin load, workspace
  pre-create, model build;
- boot phase 2 (`_build_data_plane`): the inbound Redis pub/sub listener + the
  SSE event publisher;
- `_build_checkpointer`: the AsyncPostgresSaver (wrapped with loud-failure
  checkpoint writes) + the startup claimed-inbound reconcile;
- `_build_graph`: the compiled graph + dangling tool_use repair + the
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
"""

import os
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
from shared.log import init_agent_process
from shared.paths import workspace_dir
from shared.redis_client import get_async_redis
from shared.redis_listener import RedisInboundListener

from . import _boot_timing
from .graph import build_graph
from .graph._context import AvaContext
from .mcp_daemon import _MCPDaemon
from .startup import (
    _notify_screen_capture_at_startup,
    _reconcile_claimed_inbounds_at_startup,
    _repair_dangling_tool_use_at_startup,
    _wrap_saver_writes_with_loud_failure,
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
    # enter_starting_state is called early in __main__.py before the heavy
    # import chain; status is already 'starting' by the time we reach here (the
    # boot stage). leave_starting_state bumps it to 'running' later in the claim
    # node (the run loop going live).
    init_agent_process(agent_id=agent_id)

    mcp_daemon = _MCPDaemon(agent_id)
    await mcp_daemon.spawn()

    # ── trace recording init (OTLP export to the local collector sidecar) ──
    # Must run before build_chat_model() so OpenLLMetry can auto-instrument the
    # LangChain BaseChatModel + LangGraph CompiledGraph at construction time.
    from shared.trace import initialize_tracing

    initialize_tracing()
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
    # Pre-create the per-agent workspace at startup — unconditional, not gated
    # by settings.agent.workspace_in_system_prompt: the folder is the relative-path
    # base for ava.files / ava.shell.run, so it must exist even when the
    # prompt section advertising it is off (bench runners).
    workspace_dir(agent_id)
    ava._extend.scan_and_load()
    # ── Screen capture startup check ──
    # When the converge step detected no screen capture permission, an agent
    # notifies the user once (idempotent — clears the status file after).
    # Must run after SDK init so ava.ui.notify is available.
    await _notify_screen_capture_at_startup()

    # ── MCP daemon startup ──
    # The in-process exec worker thread reaches the daemon via
    # ava.mcps._get_remote_client, which derives the socket path from
    # ava.self.AGENT_ID (set by establish above) and connects iff the socket
    # exists. Readiness is awaited just before the graph goes live.

    # Per-agent config, framework-scope: applied BEFORE build_chat_model so
    # `settings.lm.llm_model` reflects it if this agent runs a different model.
    # Plugin-scope is deferred until after build_graph's bind_from_disk has
    # populated _PLUGIN_CONFIGS — see the second apply_config_overlay call below.
    _apply_per_agent_framework_config(config_overlay, birth_config)

    llm = build_chat_model(turn_settings.lm.llm_model)
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


async def _checkpoint_schema_present(db_pool: AsyncConnectionPool[psycopg.AsyncConnection]) -> bool:
    """Whether every LangGraph checkpoint table already exists.

    The agent boot SKIPS `checkpointer.setup()` when they do (Task #1236): the
    runner process dials as the least-privilege `ava_runner` role, which holds
    no CREATE on the schema — by design — and Postgres refuses
    `CREATE TABLE IF NOT EXISTS` for such a role even on existing tables.
    setup() is otherwise a semantic no-op on an up-to-date schema (the
    IF NOT EXISTS creates + a pending-migration scan that finds none), so the
    skip loses nothing. The gateway-side setup() calls (shared/checkpoint.py,
    dialing the main role) remain the owner of langgraph's own migrations.

    All four tables must be present: a half-created schema (a crash between
    setup()'s creates) must NOT be skipped — it should run setup() and fail
    loudly on whatever is missing, not limp on.
    """
    async with db_pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT to_regclass('public.checkpoints') IS NOT NULL"
                " AND to_regclass('public.checkpoint_blobs') IS NOT NULL"
                " AND to_regclass('public.checkpoint_writes') IS NOT NULL"
                " AND to_regclass('public.checkpoint_migrations') IS NOT NULL"
            )
        ).fetchone()
    return bool(row and row[0])


async def _build_checkpointer(
    db_pool: AsyncConnectionPool[psycopg.AsyncConnection], agent_id: int
) -> AsyncPostgresSaver:
    """Saver + reconcile: wrap checkpoint writes with loud failure, then
    resolve any 'claimed' inbounds left by a previous process of this agent.

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
    saver_pool = cast(AsyncConnectionPool[psycopg.AsyncConnection[DictRow]], db_pool)
    checkpointer = AsyncPostgresSaver(conn=saver_pool, serde=build_checkpoint_serde())
    if not await _checkpoint_schema_present(db_pool):
        # First boot on a fresh schema: create the checkpoint tables. Skipped
        # when they exist — the runner role holds no CREATE (Task #1236), and
        # setup() would be a no-op anyway.
        await checkpointer.setup()
    _wrap_saver_writes_with_loud_failure(checkpointer, agent_id)
    await _reconcile_claimed_inbounds_at_startup(db_pool, checkpointer, agent_id)
    _boot_timing.mark("db_reconcile")
    return checkpointer


async def _build_graph(
    checkpointer: AsyncPostgresSaver,
    config_overlay: dict[str, object] | None,
    agent_id: int,
) -> CompiledStateGraph[BaseAgentState, AvaContext, BaseAgentState, BaseAgentState]:
    """Build the graph, repair a dangling tool_use, apply the plugin-scope
    config overlay, and write the effective-config snapshot.

    A hard cancel (SIGTERM / restart / stop kill -> asyncio.CancelledError)
    can leave a tool_use committed without its paired tool_result; repair it
    before ainvoke so resurrect doesn't loop on provider 400s (agent 167).

    Plugin-scope overlay runs after build_graph's bind_from_disk has populated
    _PLUGIN_CONFIGS; framework-scope already ran in `_boot_agent_process`
    before build_chat_model. The effective_config snapshot is written after
    both halves, so it reflects the actually effective config.
    """
    graph = build_graph(checkpointer)
    await _repair_dangling_tool_use_at_startup(graph, agent_id)
    if config_overlay:
        from shared.plugin_config_registry import apply_config_overlay

        apply_config_overlay(config_overlay, scope="plugin")
    _write_effective_config_to_restart_completed(agent_id)
    return graph
