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
    node covers (a compaction segment's head, the not yet summarized tail).

    `context_tokens` is what the block occupies in the context (None while no request has read
    it), `generation_tokens` what the model generated for it (AI blocks only), `estimated` whether
    any of that was a share rather than the provider's own number (None with `context_tokens`).
    A thinking / output(text) / call block is its share of the turn's AIMessage, so estimated.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["inbound", "text", "note", "thinking", "call", "output"]
    i0: int
    i1: int
    start: datetime
    end: datetime
    source: str | None
    preview: str
    parent: str | None
    context_tokens: int | None
    generation_tokens: int | None
    estimated: bool | None


class RunTimelineEvent(BaseModel):
    """A lifecycle marker from the audit record (spawn, restart, terminate)."""

    model_config = ConfigDict(frozen=True)

    ts: datetime
    kind: str
    label: str | None


class RunTimelineRequest(BaseModel):
    """One LLM request of the agent: an AIMessage carrying `usage_metadata`.

    `idx` is the AIMessage's index in the stitched history; `ts` the time the request was sent
    (the read time of the message before it, the start of the turn's thinking block);
    `session` the zero-based compaction segment it was sent in; `input_tokens` the provider's
    total input tokens of that request, the size of its context, and `output_tokens` what it
    generated (both the provider's own numbers, never estimated).

    `added_tokens` is what newly entered the context for this request: the token sum of the
    messages first read by it, i.e. those from the previous request's AIMessage (its output is
    re-sent) up to the message before this one; for a session's first request, from the session's
    first message. The segment head (system prompt) is not counted. `added_estimated` is True when
    any of those counts is a share rather than the provider's own number. `added_from` / `added_to`
    are that message range as indices into the stitched history, half-open (`added_to` is the
    request's own `idx`); the two are equal when the request read nothing new.
    """

    model_config = ConfigDict(frozen=True)

    idx: int
    ts: datetime
    session: int
    input_tokens: int
    output_tokens: int
    added_tokens: int
    added_estimated: bool
    added_from: int
    added_to: int


class RunTimelineResponse(BaseModel):
    """GET /api/agents/{agent_id}/run-timeline response.

    `lifetime` is the agent's whole extent — the earliest and latest of its
    messages and understanding nodes — and the default window; None when it has
    neither. `nodes` are the tree's nodes intersecting the window, every level;
    `units` are layer 0 intersecting it. `events` are optional lifecycle markers
    in the window; they play no part in the extent. `requests` are the agent's LLM requests
    sent in the window (the context-size row).
    """

    model_config = ConfigDict(frozen=True)

    agent_id: int
    window: RunTimelineWindow
    lifetime: RunTimelineWindow | None
    nodes: list[RunTimelineNode]
    units: list[RunTimelineUnit]
    events: list[RunTimelineEvent]
    requests: list[RunTimelineRequest]


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


class RunTimelineContext(ContextBreakdownResponse):
    """GET /api/agents/{agent_id}/run-timeline/context — what one LLM request's context held.

    The breakdown of the request `request` (`categories` sum to its `input_tokens`), plus where
    it sits: `session` of `sessions` compaction segments (zero-based) and the time it was sent.
    """

    request: int
    session: int
    sessions: int
    ts: datetime
