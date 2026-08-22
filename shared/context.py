"""Run-scoped dependency context — single place to assemble what a request needs.

Used in two layered roles:

1. **LangGraph node DI** (`Runtime[AvaContext]`). `agent/loop.py` and
   any embedding driver build an `AvaContext` and pass it via
   `graph.ainvoke(input, context=ctx)`. Graph nodes have signature
   `(state, runtime: Runtime[AvaContext], config: RunnableConfig)` and
   access deps as `runtime.context.X`.

2. **Non-graph entry points** (gateway lifespan / daemon `run()` / cli /
   eval driver). The gateway lifespan builds an `AvaContext` and stores it on
   `app.state.ctx`; daemons build their own. These carry connection-string
   config + caller-built handles. Threading the context through every read
   site is intentionally NOT pursued — most `settings.X` reads are connection
   strings already centralized in test conftest, so the payoff does not justify
   the blast radius.

The two roles share a single dataclass — LangGraph's `Context` is any
Python object, so non-graph fields don't bother the graph runtime.

Lifecycle of fields:
- **String config** (`db_url`, `events_channel`, ...) are immutable
  per-process snapshots taken at build time. Defaults read live `settings.X`
  so callers that don't pass them explicitly get the conftest-set value. The
  defaults are intentionally kept (not dropped) — see the refactor doc.
- **Handles** (`llm`, `redis_sync`, `ops_pool`, `inbound_listener`) are
  caller-built and caller-managed. Graph nodes that touch a handle expect it
  non-None. Non-graph entry points (gateway lifespan, daemons, cli) populate
  the handles relevant to their path and leave the rest None.

`frozen=True`: context is read-only during a run. If you need mutable
state, split it into a separate dataclass.
"""

from dataclasses import dataclass, field
from pathlib import Path

import redis as _redis_sync
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.runnables import RunnableConfig
from psycopg_pool import AsyncConnectionPool

from shared.config import settings
from shared.event_publisher import AgentEventPublisher
from shared.redis_listener import RedisInboundListener


def agent_id_from_config(
    config: RunnableConfig,
) -> int:  # returns agent_id (LangGraph config key name must be agent_id)
    """Read agent_id (required) from RunnableConfig (LangGraph checkpointer standard).

    LangGraph's `RunnableConfig` is `TypedDict(total=False)` — all fields are
    NotRequired, pyright does not allow direct `config["configurable"]["thread_id"]`
    indexing. This helper centralizes type: ignore + int conversion + fail-fast:
    missing 'configurable' or missing 'agent_id' raises KeyError immediately,
    no fallback.
    """
    return int(config["configurable"]["thread_id"])  # type: ignore[typeddict-item]  # LangGraph mandates key name, value = agent_id


@dataclass(frozen=True)
class AvaContext:
    """Run-scoped dependency bundle — see module docstring."""

    # ── handles (caller-built; all Optional so non-graph entry points can
    # build context with just string config) ──

    llm: BaseChatModel | None = None
    """LLM provider (Anthropic / DeepSeek / etc). Required by graph runtime
    (llm_node + claim node's compact path); graph entry points assert
    non-None at function start. Non-graph callers (gateway lifespan,
    daemons, cli) leave it None."""

    event_publisher: AgentEventPublisher | None = None
    """Best-effort SSE event fan-out (chat / reasoning / code deltas, exec
    output chunks, timeline snapshots). Required by graph runtime; graph entry
    points assert non-None. `emit()` is non-blocking, so a slow central Redis
    degrades the live view instead of stalling the agent's control flow (the
    exec poll loop that watches cancel/deadline, the llm stream loop).
    Non-graph callers leave it None."""

    redis_sync: _redis_sync.Redis | None = None
    """Optional sync redis client for non-graph entry points (gateway
    background tasks, cli, daemons). Graph nodes never touch it."""

    ops_pool: AsyncConnectionPool | None = None
    """Pool for all kernel-side transactional SQL (claim_inbound_batch /
    reconcile_claimed_inbounds / mark_agent_status / claim_agent_row /
    _has_pending_inbound). The
    pool's `check_connection` health-checks every borrowed conn and
    transparently reconnects when the remote PG / PgBouncer evicts an idle
    conn — single-conn alternative silently dies after server idle timeout
    (agent 57 lost 5h of checkpoints exactly this way). Eval container path does
    not need an inbound queue (graph runs one round of ainvoke per case);
    pass None so _claim takes the container early-return path without
    touching the queue."""

    inbound_listener: RedisInboundListener | None = None
    """Dedicated long-lived inbound wake listener with auto-reconnect.

    A `RedisInboundListener` (Redis pub/sub). Lives outside `ops_pool`
    because the subscription requires a dedicated long-lived connection —
    Redis pub/sub needs its own connection for `get_message()` and cannot
    share pool conns. None in container mode (no inbound queue)."""

    hosted: bool = False
    """True when this run's turns are hosted as tasks inside the agent-runner
    rather than owned by a process of their own
    (`future/infra/agent-runner-as-server.md`).

    It changes exactly one decision: what the claim node does when it finds
    nothing to claim. A process agent parks there — flips its row to `idling`
    and blocks on the inbound pub/sub — because the process has nothing else to
    do and must stay alive. A hosted agent has no process to park: the host ends
    the turn task and creates a new one when the dispatcher sees a wake, so idle
    genuinely costs nothing.

    Deliberately a context field, not a `settings.daemon.runner_mode` read at
    the claim site: the mode belongs to the runtime that BUILT this context, so
    an eval-container or test run can stay process-shaped regardless of what
    the cluster's flag says, and the claim node keeps taking all its
    dependencies from one place. Left False by every current caller, so the
    branch is unreachable until the host service builds a context with it."""

    # ── string-level config ──
    #
    # String-level config. Defaults read live `settings.X` so callers that
    # don't pass them explicitly get the conftest-set value. Kept by design:
    # threading each of these through every read site was judged not worth the
    # blast radius (see the module docstring).

    db_url: str = field(default_factory=lambda: settings.data_plane.db_url)
    redis_url: str = field(default_factory=lambda: settings.data_plane.redis_url)
    events_channel: str = field(default_factory=lambda: settings.data_plane.events_channel)
    ava_home: Path = field(default_factory=lambda: settings.general.ava_home)
