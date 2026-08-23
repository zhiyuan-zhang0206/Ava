"""fleet graph.

Split out of the former monolithic ops/schemas.py; FastAPI registers these
unchanged, so the OpenAPI codegen is byte-identical to the wire before.
"""

from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
)

from shared.agents import AgentStatus


class FleetGraphNode(BaseModel):
    """One agent node in the fleet force-directed graph."""

    model_config = ConfigDict(frozen=True)

    agent_id: int
    label: str | None
    status: AgentStatus
    liveness_state: Literal["online", "offline", "unknown"]
    spawner: str
    machine: str | None
    # Recent-work score over the selected window: in_total * 0.1 + out_total * 1.0
    # summed from the agent's llm_usage events. Drives node size in the graph
    # (output tokens weigh 10x input — the agent's actual produced work).
    node_score: float
    # Cumulative tokens consumed (input + output, all-time) — summed from the
    # agent's llm_usage events. Reference figure shown in the node tooltip.
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

    `stale` is the degraded-signal flag (R4 layer 2, audit gateway.md
    P2-10): the graph query is canceled (statement timeout under load) the
    response is an EMPTY graph with `stale=True` — the frontend must render
    "data temporarily unavailable" instead of an empty fleet, because an
    empty graph and a failed graph are not the same state.
    """

    model_config = ConfigDict(frozen=True)

    nodes: list[FleetGraphNode]
    edges: list[FleetGraphEdge]
    stale: bool = False
    truncated: bool = False
