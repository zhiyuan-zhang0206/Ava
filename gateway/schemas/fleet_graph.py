"""fleet graph.

FastAPI exports these projections with shared enum schema references.
The generated frontend contract preserves their JSON wire values.
"""

from datetime import datetime

from pydantic import (
    BaseModel,
    ConfigDict,
)

from base.api_contracts.agent_contract import AgentStatus, LivenessState


class FleetGraphNode(BaseModel):
    """One agent node in the fleet force-directed graph."""

    model_config = ConfigDict(frozen=True)

    agent_id: int
    label: str | None
    status: AgentStatus
    liveness_state: LivenessState
    spawner: str
    machine: str | None
    # Recent-work score over the selected window: in_total * 0.1 + out_total * 1.0
    # summed from the agent's llm_usage events. Drives node size in the graph
    # (output tokens weigh 10x input — the agent's actual produced work).
    node_score: float
    # Tokens consumed in the retained window (input + output, 7d), restart-proof
    # via Prometheus increase(). Reference figure shown in the node tooltip.
    total_tokens: int


class FleetGraphEdge(BaseModel):
    """One directed edge in the fleet graph: from_agent → to_agent."""

    model_config = ConfigDict(frozen=True)

    from_agent: int
    to_agent: int
    event_type: str
    weight: float
    event_count: int
    last_seen_at: str  # ISO-8601


class FleetGraphResponse(BaseModel):
    """GET /api/fleet/graph response.

    `telemetry_stale` says the heartbeat guard was old or missing while this
    otherwise complete graph was read. `snapshot_at` records when the graph
    snapshot was produced; a response served from the short poll cache keeps
    that timestamp so clients can show its age.
    """

    model_config = ConfigDict(frozen=True)

    nodes: list[FleetGraphNode]
    edges: list[FleetGraphEdge]
    telemetry_stale: bool = False
    snapshot_at: datetime | None = None
