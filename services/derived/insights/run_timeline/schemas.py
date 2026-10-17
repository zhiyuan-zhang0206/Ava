# meiva:link services/derived/insights/run_timeline/docs/range-projection.meiva.okf.md
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from base.agents.history.context_response import ContextBreakdownResponse


class RunTimelineWindow(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        json_schema_extra={"description": "The inclusive time window one response covers."},
    )

    from_: datetime = Field(serialization_alias="from")
    to: datetime


class RunTimelineUsage(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        json_schema_extra={
            "description": "The agent's own cost over a message span: its AIMessages' `usage_metadata`, summed.\n\n`input` is the provider's total input tokens (cache reads and writes included).\n`cost_usd` sums the usage-time cost recorded on the AIMessages (`meiva_usage`); `cost_calls` is\nhow many of `calls` carry one, the rest (older messages, unpriced models) being unknown, not\nestimated."
        },
    )

    calls: int
    input: int
    cache_read: int
    output: int
    cache_write: int
    cost_usd: float
    cost_calls: int


class RunTimelineGeneration(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        json_schema_extra={
            "description": "What generating a node cost: the usage and wall time of its understanding calls."
        },
    )

    calls: int
    input: int
    cache_read: int
    output: int
    seconds: float


class RunTimelineNode(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        json_schema_extra={
            "description": "One understanding-tree node.\n\n`level` is the engine level, stable across windows: 1 is the finest (leaves),\neach level up groups the one below. `span_start`..`span_end` is the inclusive\nmessage-index span in the stitched history, the indices the raw-message route\nreads. `generation` is None for a node with no understanding-call record.\n`context_tokens` is the sum of the context tokens of the messages the span covers (None while\nno request has read any of them), `estimated` whether any of that was a share rather than the\nprovider's own number (None with `context_tokens`)."
        },
    )

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
    model_config = ConfigDict(
        frozen=True,
        json_schema_extra={
            "description": "One layer-0 block (see `base.agents.history.hierarchy.units`).\n\nA message unit, except that a work unit is served as its parts: `thinking` (the model's\ngeneration for the turn), `call` (an instant at the end of the stream) and `output` (the\nexecution). `start` / `end` are the extent on the read times of the messages; `i0`..`i1` the\ninclusive message-index span of the block's unit. Blocks without a time are not served.\n`parent` is the level-1 node whose span holds the block's first message, None for a block no\nnode covers (a compaction segment's head, the not yet summarized tail). `inbound_id` is the\n`inbound_messages` row an inbound or note block was made from (the checkpoint's `meiva_inbound_id`), None\nfor any other block and for a message that carries no stamp.\n\n`context_tokens` is what the block occupies in the context (None while no request has read\nit), `generation_tokens` what the model generated for it (AI blocks only), `estimated` whether\nany of that was a share rather than the provider's own number (None with `context_tokens`).\nA thinking / output(text) / call block is its share of the turn's AIMessage, so estimated.\n\n`context_total` is the context through this block (what the Context size row draws; None while\nno request has read it): its session's head and everything up to and including the block, each\nmessage at the weight it was read with. The blocks of one AIMessage each add their share, so the\nfirst one starts from the `input_tokens` of the request that produced the message and the last\nends at the context through the whole message. `session` is the zero-based compaction segment\n(the total starts over in each). `request` is the usage of the LLM request the block's AIMessage\nwas (input, output, cache, cost), repeated on each of its turn blocks; None on any other block."
        },
    )

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


LinkKind = Literal["send_message", "spawn", "fork", "terminate", "restart", "resurrect", "notice"]


class RunTimelineLink(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        json_schema_extra={
            "description": "One event between two agents, or between an agent and the user. `sender` did it to `receiver`; None is the user.\n\nA `notice` is an agent posting a notice to the user (`agent_notices`, the structured\nagent-to-user channel): its receiver is None and `notice_id` its row. The text of a message or\nnotice is not in the list; `/run-timeline/link-content` serves it for the one that is selected.\n\n`inbound_id` names the receiver's inbound row, when the event was delivered as one (a message, terminate, restart, resurrect, fork); `fork_from` is the agent\na fork was copied from (fork only; the sender is the agent that executed the fork)."
        },
    )

    kind: LinkKind
    ts: datetime
    sender: int | None
    receiver: int | None
    inbound_id: int | None
    fork_from: int | None
    notice_id: int | None


class RunTimelineLinkContent(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        json_schema_extra={
            "description": "GET /api/insights/run-timeline/link-content: the full text of one chat message, or the title and text of one notice."
        },
    )

    title: str | None
    content: str


class RunTimelineLinks(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        json_schema_extra={
            "description": "GET /api/insights/run-timeline/links response: the events between agents (and with the user) with an end in the asked agents, oldest first."
        },
    )

    links: list[RunTimelineLink]


class RunTimelineResponse(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        json_schema_extra={
            "description": "GET /api/agents/{agent_id}/run-timeline response.\n\n`lifetime` is the agent's whole extent \u2014 the earliest and latest of its\nmessages and understanding nodes \u2014 and the default window; None when it has\nneither. `nodes` are the tree's nodes intersecting the window, every level;\n`units` are layer 0 intersecting it. With `max_units`, a denser window omits\nall message detail and reports its count in `units_omitted`; it never silently\ntruncates the window. `snapshot` pins later window, message and context reads\nto this history until it expires or is evicted (HTTP 409)."
        },
    )

    agent_id: int
    window: RunTimelineWindow
    lifetime: RunTimelineWindow | None
    nodes: list[RunTimelineNode]
    units: list[RunTimelineUnit]
    units_omitted: int = 0
    snapshot: str | None = None


RunTimelinePartKind = Literal[
    "think", "text", "call", "out", "note", "compact", "inbound", "attach", "prompt"
]


class RunTimelineMessagePart(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        json_schema_extra={
            "description": "One part of a raw message: its text, clipped to the per-part budget unless `full` was asked."
        },
    )

    kind: RunTimelinePartKind
    chars: int
    text: str
    text_truncated: bool


class RunTimelineMessage(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        json_schema_extra={
            "description": "One raw message of the stitched history, split into its parts.\n\n`context_tokens` is what the message occupies in the context (None while no request has read it),\n`estimated` whether that is a share rather than the provider's own number (None with it)."
        },
    )

    idx: int
    ts: datetime | None
    source: str | None
    parts: list[RunTimelineMessagePart]
    context_tokens: int | None
    estimated: bool | None


class RunTimelineMessages(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        json_schema_extra={
            "description": "GET /api/agents/{agent_id}/run-timeline/messages response.\n\n`next_start` is the index to ask for next when the range was cut at `limit`\nmessages, else None."
        },
    )

    messages: list[RunTimelineMessage]
    next_start: int | None


class RunTimelineContext(ContextBreakdownResponse):
    model_config = ConfigDict(
        json_schema_extra={
            "description": "GET /api/agents/{agent_id}/run-timeline/context — what one LLM request's context held.\n\nThe breakdown of the request `request` (`categories` sum to its `input_tokens`), plus where\nit sits: `session` of `sessions` compaction segments (zero-based) and the time it was sent."
        },
    )

    request: int
    session: int
    sessions: int
    ts: datetime
