"""Wire models of the cluster view."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

AgentKind = Literal["root", "spawn", "fork"]


class ClusterWindow(BaseModel):
    """The window one response covers: `from` inclusive, `to` exclusive."""

    model_config = ConfigDict(frozen=True)

    from_: datetime = Field(serialization_alias="from")
    to: datetime


class ClusterAgentCost(BaseModel):
    """One agent's recorded LLM spend in one bucket."""

    model_config = ConfigDict(frozen=True)

    agent_id: int
    calls: int
    cost_usd: float


class CurveBucket(BaseModel):
    """One non-empty time bucket of the cluster curves; an absent bucket is all zeros.

    `ts` is the bucket's start. `costs` has one entry per agent that made an LLM call in it, so
    `len(costs)` is the active agent count. `queue_*_seconds` is the claimed-minus-created time
    of the inbound rows of the messages sent in the bucket (None when none of them was claimed),
    `queue_samples` how many rows it is over.
    """

    model_config = ConfigDict(frozen=True)

    ts: datetime
    costs: list[ClusterAgentCost]
    active_agents: int
    messages: int
    queue_samples: int
    queue_p50_seconds: float | None
    queue_p95_seconds: float | None


class ClusterCurves(BaseModel):
    """GET /api/insights/cluster/curves response.

    `unpriced_calls` counts the window's LLM calls without a recorded cost: their spend is not
    in `costs`. `messages` counts agent-to-agent messages whose sender and receiver are both
    in `agent_ids`.
    """

    model_config = ConfigDict(frozen=True)

    window: ClusterWindow
    bucket_seconds: int
    agent_ids: list[int]
    unpriced_calls: int
    buckets: list[CurveBucket]


class LaneNode(BaseModel):
    """One understanding-tree node on a lane, on the node's stored time extent.

    `summary` is clipped to the first characters of the node's text.
    """

    model_config = ConfigDict(frozen=True)

    id: int
    level: int
    parent: int | None
    start: datetime
    end: datetime
    summary: str


class LaneBar(BaseModel):
    """A stretch of LLM activity: consecutive requests merged when closer than `bin_seconds`.

    A request occupies `[ts - latency, ts]` of its `llm_usage` event; a single request is a bar
    of `calls == 1`.
    """

    model_config = ConfigDict(frozen=True)

    start: datetime
    end: datetime
    calls: int
    cost_usd: float
    input_tokens: int
    output_tokens: int


class LaneEvent(BaseModel):
    """A lifecycle marker from the audit record (spawn, resurrect, restart, terminate)."""

    model_config = ConfigDict(frozen=True)

    ts: datetime
    kind: str


class AgentLane(BaseModel):
    """One agent's lane, in tree order.

    `parent` is the agent that spawned it, or the fork source of a fork, when that agent is in
    the selection (None for the selection's roots); `depth` its distance from a root.
    """

    model_config = ConfigDict(frozen=True)

    agent_id: int
    parent: int | None
    kind: AgentKind
    depth: int
    status: str
    spawned_at: datetime
    calls: int
    cost_usd: float
    nodes: list[LaneNode]
    bars: list[LaneBar]
    events: list[LaneEvent]


class LevelCount(BaseModel):
    """How many understanding nodes of the selection intersect the window at one level."""

    model_config = ConfigDict(frozen=True)

    level: int
    nodes: int


class ClusterLanes(BaseModel):
    """GET /api/insights/cluster/lanes response.

    `level` is the understanding level whose nodes the lanes carry; `auto_level` says the
    service chose it (see `lanes.choose_level`), `levels` lists every level there is something
    to show at. `bin_seconds` is the merge distance of the activity bars.
    """

    model_config = ConfigDict(frozen=True)

    window: ClusterWindow
    level: int | None
    auto_level: bool
    levels: list[LevelCount]
    bin_seconds: int
    lanes: list[AgentLane]


class MessageEdge(BaseModel):
    """One agent-to-agent message: sent by `sender` at `sent_at`, read by `receiver` at `read_at`.

    Both ends are agent-level: the edge says nothing about what the receiver was doing. `read_at`
    is when the receiver's claim step took the message (`inbound_messages.claimed_at`), None
    while unclaimed or once the inbound row has been pruned.
    """

    model_config = ConfigDict(frozen=True)

    inbound_id: int | None
    sender: int
    receiver: int
    sent_at: datetime
    read_at: datetime | None
    preview: str


class ClusterMessages(BaseModel):
    """GET /api/insights/cluster/messages response; `truncated` when more matched than `limit`."""

    model_config = ConfigDict(frozen=True)

    window: ClusterWindow
    total: int
    truncated: bool
    edges: list[MessageEdge]
