"""Wire models for the agent run timeline (the understanding tree over the message history)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RunTimelineWindow(BaseModel):
    """The inclusive time window one response covers."""

    model_config = ConfigDict(frozen=True)

    from_: datetime = Field(serialization_alias="from")
    to: datetime


class RunTimelineUsage(BaseModel):
    """The agent's own cost over a message span: its AIMessages' `usage_metadata`, summed.

    `input` is the provider's total input tokens (cache reads included).
    """

    model_config = ConfigDict(frozen=True)

    calls: int
    input: int
    cache_read: int
    output: int


class RunTimelineGeneration(BaseModel):
    """What generating a node cost: the usage and wall time of its understanding calls."""

    model_config = ConfigDict(frozen=True)

    calls: int
    input: int
    cache_read: int
    output: int
    seconds: float


class RunTimelineNode(BaseModel):
    """One understanding-tree node.

    `level` is the engine level, stable across windows: 1 is the finest (leaves),
    each level up groups the one below. `span_start`..`span_end` is the inclusive
    message-index span in the stitched history, the indices the raw-message route
    reads. `generation` is None for a node with no understanding-call record.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    level: int
    parent: str | None
    start: datetime
    end: datetime
    span_start: int
    span_end: int
    summary: str
    usage: RunTimelineUsage
    generation: RunTimelineGeneration | None


class RunTimelineUnit(BaseModel):
    """One layer-0 block (see `base.agents.history.hierarchy.units`).

    A message unit, except that a work unit is served as its parts: `thinking` (the model's
    generation for the turn), `call` (an instant at the end of the stream) and `output` (the
    execution). `start` / `end` are the extent on the read times of the messages; `i0`..`i1` the
    inclusive message-index span of the block's unit. Blocks without a time are not served.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["inbound", "text", "note", "thinking", "call", "output"]
    i0: int
    i1: int
    start: datetime
    end: datetime
    source: str | None
    preview: str


class RunTimelineEvent(BaseModel):
    """A lifecycle marker from the audit record (spawn, restart, terminate)."""

    model_config = ConfigDict(frozen=True)

    ts: datetime
    kind: str
    label: str | None


class RunTimelineResponse(BaseModel):
    """GET /api/agents/{agent_id}/run-timeline response.

    `lifetime` is the agent's whole extent — the earliest and latest of its
    messages and understanding nodes — and the default window; None when it has
    neither. `nodes` are the tree's nodes intersecting the window, every level;
    `units` are layer 0 intersecting it. `events` are optional lifecycle markers
    in the window; they play no part in the extent.
    """

    model_config = ConfigDict(frozen=True)

    agent_id: int
    window: RunTimelineWindow
    lifetime: RunTimelineWindow | None
    nodes: list[RunTimelineNode]
    units: list[RunTimelineUnit]
    events: list[RunTimelineEvent]


RunTimelinePartKind = Literal[
    "think", "text", "call", "out", "note", "compact", "inbound", "attach", "prompt"
]


class RunTimelineMessagePart(BaseModel):
    """One part of a raw message: its text, clipped to the per-part budget unless `full` was asked."""

    model_config = ConfigDict(frozen=True)

    kind: RunTimelinePartKind
    chars: int
    text: str
    text_truncated: bool


class RunTimelineMessage(BaseModel):
    """One raw message of the stitched history, split into its parts."""

    model_config = ConfigDict(frozen=True)

    idx: int
    ts: datetime | None
    source: str | None
    parts: list[RunTimelineMessagePart]


class RunTimelineMessages(BaseModel):
    """GET /api/agents/{agent_id}/run-timeline/messages response.

    `next_start` is the index to ask for next when the range was cut at `limit`
    messages, else None.
    """

    model_config = ConfigDict(frozen=True)

    messages: list[RunTimelineMessage]
    next_start: int | None
