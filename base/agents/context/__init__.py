"""Run-scoped dependency context — what one graph run is handed.

The agent host builds an `AvaContext` for each turn task and passes it via
`graph.ainvoke(input, context=ctx)`. Graph nodes have the signature
`(state, runtime: Runtime[AvaContext], config: RunnableConfig)` and read their
dependencies as `runtime.context.X`:

- **handles** the host built and owns: `llm`, `event_publisher`, `ops_pool`, the cluster
  `db` (`Database`) and `bus` (`EventBus`) — a node that touches a handle expects it non-None;
  the eval driver and tests populate only the ones their path needs;
- **`agent`**: the agent's resolved per-turn configuration (`AgentSlices`, see
  `base.host.env.agent_slices`), built by the host when the turn starts;
- **`extensions`**: the plugins' contributions, held by the host that loaded them;
- **host-held state** the graph reads or writes across turns: `turn_progress`, `relays`,
  `recall_log_key`. The host builds each once and hands the same object to every turn it runs;
  a context built without them (the eval driver, a test) carries private ones.

`frozen=True`: context is read-only during a run. If you need mutable state, split it into a
separate dataclass.
"""

import secrets
from dataclasses import dataclass, field

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.runnables import RunnableConfig
from psycopg_pool import AsyncConnectionPool

from base.agents.observation.relay_supervision import RelaySupervision
from base.agents.observation.turn_progress import TurnProgress
from base.db import Database
from base.events.live.bus import EventBus
from base.events.live.publisher import AgentEventPublisher
from base.host.env.agent_slices import AgentSlices
from base.packages.plugins.extensions import EMPTY, ExtensionRegistry


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

    # ── handles (host-built; all Optional so the eval driver and tests can build a
    # context with just the ones their path touches) ──

    llm: BaseChatModel | None = None
    """LLM provider (Anthropic / DeepSeek / etc). Required by graph runtime
    (llm_node + claim node's compact path); graph entry points assert
    non-None at function start."""

    event_publisher: AgentEventPublisher | None = None
    """Best-effort SSE event fan-out (chat / reasoning / code deltas, exec
    output chunks, timeline snapshots). Required by graph runtime; graph entry
    points assert non-None. `emit()` is non-blocking, so a slow central Redis
    degrades the live view instead of stalling the agent's control flow (the
    exec poll loop that watches cancel/deadline, the llm stream loop)."""

    ops_pool: AsyncConnectionPool | None = None
    """Pool for all kernel-side transactional SQL (claim_inbound_batch /
    reconcile_claimed_inbounds / lifecycle settlement). The
    pool's `check_connection` health-checks every borrowed conn and
    transparently reconnects when the remote PG / PgBouncer evicts an idle
    conn — single-conn alternative silently dies after server idle timeout
    (agent 57 lost 5h of checkpoints exactly this way). Eval container path does
    not need an inbound queue (graph runs one round of ainvoke per case);
    pass None so _claim takes the container early-return path without
    touching the queue."""

    db: Database | None = None
    """The cluster Postgres handle, for the code a node calls that opens its own connection."""

    bus: EventBus | None = None
    """The cluster Redis / live-events handle, for the same."""

    agent: AgentSlices | None = None
    """This agent's per-turn configuration, resolved by the host when the turn starts."""

    extensions: ExtensionRegistry = EMPTY
    """What the enabled plugins contribute (prompt sections, context notes), as the loader built it
    for this host. Empty for the eval driver and tests that run no plugins."""

    turn_progress: TurnProgress = field(default_factory=TurnProgress)
    """The turn-progress clock: graph nodes and the LLM stream mark activity on it, the host's
    stall guard, dispatcher and heartbeat read it."""

    relays: RelaySupervision = field(default_factory=RelaySupervision)
    """The impersonation relays this process spawned and its start clock, read by the claim gate
    and the host's supervision."""

    recall_log_key: bytes = field(default_factory=lambda: secrets.token_bytes(32))
    """The key behind the passive-recall log's query HMAC. One per host process, so repeated
    filter decisions can be joined within it without making low-entropy conversation text
    dictionary-reversible from telemetry."""

    def require_db(self) -> Database:
        """The cluster database handle; a context built without one fails here."""
        if self.db is None:
            raise RuntimeError("this AvaContext carries no Database (ctx.db is None)")
        return self.db

    def require_bus(self) -> EventBus:
        """The cluster event bus handle; a context built without one fails here."""
        if self.bus is None:
            raise RuntimeError("this AvaContext carries no EventBus (ctx.bus is None)")
        return self.bus

    def require_agent(self) -> AgentSlices:
        """The agent's slices; a graph run built without them fails here, not on first read."""
        if self.agent is None:
            raise RuntimeError("this AvaContext carries no AgentSlices (ctx.agent is None)")
        return self.agent
