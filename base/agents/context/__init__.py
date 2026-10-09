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
- **`identity`**: who the run acts as (`AgentIdentity`);
- **`clients`**: the lazily-built connections the process holds (SQL, Redis, gateway HTTP, and the
  SDK layer's own), closed with `close()`.
- **host-held state** the graph reads or writes across turns: `turn_progress`, `relays`,
  `recall_log_key`. The host builds each once and hands the same object to every turn it runs;
  a context built without them (the eval driver, a test) carries private ones.

The exec child holds the same type. The host puts `describe()` of the turn's context in the exec
request envelope and the child builds its instance with `from_description()`, so agent code reads
the identity the host's turn carries (`ava.context`). Only what is serializable and not secret
crosses (identity, the gateway endpoint); a connection the child needs it builds itself, on first
use, from its own settings and environment, and releases when the process ends.

`frozen=True`: context is read-only during a run. If you need mutable state, split it into a
separate dataclass.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Self

from base.agents.context.clients import ClientSet, LazyConnection
from base.agents.context.identity import AgentIdentity
from base.agents.incarnation.native_work_models import NativeWorkTarget
from base.agents.observation.relay_supervision import RelaySupervision
from base.agents.observation.turn_progress import TurnProgress
from base.agents.sdk.tally import SdkCallTally
from base.lm.call import ProviderCallBinding
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.native_process.turn_identity import HostedTurnResources
from base.lm.catalog import ModelCatalog

# The handle types are annotations only: the exec child builds this same type from its request
# envelope, and its start must not import psycopg / redis / langchain for handles it never holds.
if TYPE_CHECKING:
    import httpx
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.runnables import RunnableConfig
    from psycopg_pool import AsyncConnectionPool

    from base.db import Database
    from base.events.live.bus import EventBus
    from base.events.live.publisher import AgentEventPublisher
    from base.host.env.agent_slices import AgentSlices
    from base.packages.plugins.extensions import ExtensionRegistry


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

    llm_binding: ProviderCallBinding | None = None
    """Binding selected alongside llm; never serialized into exec or checkpoints."""

    catalog: ModelCatalog | None = None
    """Provider facts built and retained by this run's process composition root."""

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

    extensions: ExtensionRegistry | None = None
    """What the enabled plugins contribute (prompt sections, context notes), as the loader built it
    for this host. None for the eval driver and tests that run no plugins (`plugin_registry`)."""

    identity: AgentIdentity | None = None
    """Who this run acts as. The host sets it for a turn it serves; the exec child and a launched
    script get theirs from a description, an external controller's carries its lease."""

    original_incarnation: RuntimeIncarnation | None = None
    """The exact admission this run received; never refreshed from a replacement row."""

    native_work: NativeWorkTarget | None = None
    """The original invocation target, retained across database recovery."""

    hosted_resources: HostedTurnResources | None = None
    """The actual turn's domains and late completions, shared by copied contexts."""

    def require_original_incarnation(self, agent_id: int) -> RuntimeIncarnation:
        """Require an explicit original admission for an owned native operation."""
        if self.original_incarnation is None:
            raise RuntimeError("this AvaContext carries no original RuntimeIncarnation")
        return self.original_incarnation.require_agent(agent_id)

    clients: ClientSet = field(default_factory=ClientSet)
    """The connections this run's process holds. Built on first use; the owner of the context
    (the exec child, a launched script, an attachment, the host) closes them."""

    sdk_calls: SdkCallTally | None = None
    """This execution's unsampled public-call tally. Never serialized into requests or state."""

    @property
    def sql(self) -> LazyConnection:
        """The cluster database as one autocommit connection."""
        return self.clients.sql

    @property
    def redis(self) -> LazyConnection:
        """The cluster Redis client."""
        return self.clients.redis

    @property
    def gateway(self) -> httpx.Client:
        """The HTTP client for the gateway API."""
        return self.clients.gateway

    def require_identity(self) -> AgentIdentity:
        """The identity of this run; a context built without one fails here."""
        if self.identity is None:
            raise RuntimeError("this AvaContext carries no AgentIdentity (ctx.identity is None)")
        return self.identity

    def describe(self) -> dict[str, Any]:
        """The serializable, non-secret description an exec request envelope carries."""
        return {
            "identity": self.require_identity().describe(),
            "gateway_url": self.clients.gateway_url,
        }

    @classmethod
    def from_description(
        cls,
        description: dict[str, Any],
        *,
        clients: ClientSet,
        original_incarnation: RuntimeIncarnation | None = None,
    ) -> Self:
        """Rebuild identity with clients and original admission supplied by the child's root."""
        return cls(
            identity=AgentIdentity.from_description(description["identity"]),
            original_incarnation=original_incarnation,
            clients=clients,
        )

    def plugin_registry(self) -> ExtensionRegistry:
        """What the plugins contribute; an empty registry when this run loaded none."""
        from base.packages.plugins.extensions import EMPTY

        return EMPTY if self.extensions is None else self.extensions

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

    def require_catalog(self) -> ModelCatalog:
        """The process-owned provider catalog; a run without one fails explicitly."""
        if self.catalog is None:
            raise RuntimeError("this AvaContext carries no ModelCatalog (ctx.catalog is None)")
        return self.catalog
