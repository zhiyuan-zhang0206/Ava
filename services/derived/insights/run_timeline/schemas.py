"""Wire models for the agent run timeline (the understanding tree over the message history)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from base.agents.history.context_response import ContextBreakdownResponse


class RunTimelineWindow(BaseModel):
    """The inclusive time window one response covers."""

    model_config = ConfigDict(frozen=True)

    from_: datetime = Field(serialization_alias="from")
    to: datetime


class RunTimelineUsage(BaseModel):
    """The agent's own cost over a message span: its AIMessages' `usage_metadata`, summed.

    `input` is the provider's total input tokens (cache reads and writes included).
    `cost_usd` sums the usage-time cost recorded on the AIMessages (`ava_usage`); `cost_calls` is
    how many of `calls` carry one, the rest (older messages, unpriced models) being unknown, not
    estimated.
    """

    model_config = ConfigDict(frozen=True)

    calls: int
    input: int
    cache_read: int
    output: int
    cache_write: int
    cost_usd: float
    cost_calls: int


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
    `context_tokens` is the sum of the context tokens of the messages the span covers (None while
    no request has read any of them), `estimated` whether any of that was a share rather than the
    provider's own number (None with `context_tokens`).
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
    context_tokens: int | None
    estimated: bool | None


class RunTimelineUnit(BaseModel):
    """One layer-0 block (see `base.agents.history.hierarchy.units`).

    A message unit, except that a work unit is served as its parts: `thinking` (the model's
    generation for the turn), `call` (an instant at the end of the stream) and `output` (the
    execution). `start` / `end` are the extent on the read times of the messages; `i0`..`i1` the
    inclusive message-index span of the block's unit. Blocks without a time are not served.
    `parent` is the level-1 node whose span holds the block's first message, None for a block no
    node covers (a compaction segment's head, the not yet summarized tail). `inbound_id` is the
    `inbound_messages` row an inbound or note block was made from (the checkpoint's `ava_inbound_id`), None
    for any other block and for a message that carries no stamp.

    `context_tokens` is what the block occupies in the context (None while no request has read
    it), `generation_tokens` what the model generated for it (AI blocks only), `estimated` whether
    any of that was a share rather than the provider's own number (None with `context_tokens`).
    A thinking / output(text) / call block is its share of the turn's AIMessage, so estimated.

    `context_total` is the context through this block (what the Context size row draws; None while
    no request has read it): its session's head and everything up to and including the block, each
    message at the weight it was read with. The blocks of one AIMessage each add their share, so the
    first one starts from the `input_tokens` of the request that produced the message and the last
    ends at the context through the whole message. `session` is the zero-based compaction segment
    (the total starts over in each). `request` is the usage of the LLM request the block's AIMessage
    was (input, output, cache, cost), repeated on each of its turn blocks; None on any other block.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["inbound", "text", "note", "thinking", "call", "output"]
    i0: int
    i1: int
    start: datetime
    end: datetime
    source: str | None
    inbound_id: int | None
    preview: str
    parent: str | None
    context_tokens: int | None
    generation_tokens: int | None
    estimated: bool | None
    session: int
    context_total: int | None
    request: RunTimelineUsage | None


LinkKind = Literal["send_message", "spawn", "fork", "terminate", "restart", "resurrect"]


class RunTimelineLink(BaseModel):
    """One event between two agents. `sender` did it to `receiver`.

    `inbound_id` names the receiver's inbound row, when the event was delivered as one (a message, terminate, restart, resurrect, fork); `fork_from` is the agent
    a fork was copied from (fork only; the sender is the agent that executed the fork); `preview` is
    the start of the message.
    """

    model_config = ConfigDict(frozen=True)

    kind: LinkKind
    ts: datetime
    sender: int
    receiver: int
    inbound_id: int | None
    fork_from: int | None
    preview: str | None


class RunTimelineLinks(BaseModel):
    """GET /api/insights/run-timeline/links response: the agent-to-agent events with an end in the asked agents, oldest first."""

    model_config = ConfigDict(frozen=True)

    links: list[RunTimelineLink]


class RunTimelineResponse(BaseModel):
    """GET /api/agents/{agent_id}/run-timeline response.

    `lifetime` is the agent's whole extent — the earliest and latest of its
    messages and understanding nodes — and the default window; None when it has
    neither. `nodes` are the tree's nodes intersecting the window, every level;
    `units` are layer 0 intersecting it.
    """

    model_config = ConfigDict(frozen=True)

    agent_id: int
    window: RunTimelineWindow
    lifetime: RunTimelineWindow | None
    nodes: list[RunTimelineNode]
    units: list[RunTimelineUnit]


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
    """One raw message of the stitched history, split into its parts.

    `context_tokens` is what the message occupies in the context (None while no request has read it),
    `estimated` whether that is a share rather than the provider's own number (None with it).
    """

    model_config = ConfigDict(frozen=True)

    idx: int
    ts: datetime | None
    source: str | None
    parts: list[RunTimelineMessagePart]
    context_tokens: int | None
    estimated: bool | None


class RunTimelineMessages(BaseModel):
    """GET /api/agents/{agent_id}/run-timeline/messages response.

    `next_start` is the index to ask for next when the range was cut at `limit`
    messages, else None.
    """

    model_config = ConfigDict(frozen=True)

    messages: list[RunTimelineMessage]
    next_start: int | None


class RunTimelineContext(ContextBreakdownResponse):
    """GET /api/agents/{agent_id}/run-timeline/context — what one LLM request's context held.

    The breakdown of the request `request` (`categories` sum to its `input_tokens`), plus where
    it sits: `session` of `sessions` compaction segments (zero-based) and the time it was sent.
    """

    request: int
    session: int
    sessions: int
    ts: datetime
