"""Wire models for the event-driven agent run timeline."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RunTimelineWindow(BaseModel):
    """The inclusive Loki window used to derive one timeline."""

    model_config = ConfigDict(frozen=True)

    from_: datetime = Field(serialization_alias="from")
    to: datetime


class RunTimelineMeta(BaseModel):
    """Run-level totals derived from the rows in a timeline window."""

    model_config = ConfigDict(frozen=True)

    n_turns: int
    wall_span_s: float
    active_s: float
    tokens_in: int
    tokens_out: int
    cost_usd: float
    n_exec_failed: int
    n_compact: int
    n_restart: int
    fallback_turns: int
    unmatched_turns: int


class RunTimelineLlm(BaseModel):
    """Absolute LLM usage for a turn or an aggregated time bucket."""

    model_config = ConfigDict(frozen=True)

    calls: int
    in_total: int
    cache_read: int
    out_total: int
    reasoning: int
    latency_ms: float
    cost_usd: float
    model: str | None


class RunTimelineExec(BaseModel):
    """Tool and outcome of one execution event; exec events have no duration."""

    model_config = ConfigDict(frozen=True)

    tool: str
    ok: bool


class RunTimelineRow(BaseModel):
    """A completed turn, or a bucket made from contiguous completed turns."""

    model_config = ConfigDict(frozen=True)

    turn: int | None
    n_turns: int
    start: datetime
    end: datetime
    active_s: float
    trace_id: str | None
    checkpoint_id: str | None
    ok: bool | None
    llm: RunTimelineLlm
    execs: list[RunTimelineExec]
    anomalies: list[str]
    tags: list[str]


class RunTimelineEvent(BaseModel):
    """An event-rail marker relevant to the selected session window."""

    model_config = ConfigDict(frozen=True)

    ts: datetime
    kind: str
    trace_id: str | None
    label: str | None


class RunTimelineSummary(BaseModel):
    """The raw-context summary — shown when a run has no hierarchical layers."""

    model_config = ConfigDict(frozen=True)

    text: str


class RunTimelineLayerNode(BaseModel):
    """One narrative-layer node (depth 0 = overview, 1 = stage, 2 = block).

    Nodes form a tree via ``parent``; ``summary`` is the node's single text
    (the hierarchy-understanding rule: humans and agents read the same text).
    Present only when hierarchical summaries exist for the window; turn/call
    detail stays on ``rows`` (depth 3/4 of the same naming).
    """

    model_config = ConfigDict(frozen=True)

    id: str
    depth: int
    parent: str | None
    start: datetime
    end: datetime
    summary: str


class RunTimelineInbound(BaseModel):
    """One chat delivery fact (inbound_messages row) inside the window.

    The arrow source for multi-agent compare views: ``source`` carries the
    envelope contract (``agent:<id>`` / ``user`` / ...); ``inbound_id`` is
    ava_inbound_id, the identity the console item stream already exposes.
    """

    model_config = ConfigDict(frozen=True)

    ts: datetime
    source: str
    inbound_id: int


class RunTimelinePendingSpan(BaseModel):
    """One pending stretch -- window activity no sealed understanding layer covers.

    The layer-track placeholder source (B, 2026-09-18): rendered de-emphasized
    so an uncovered stretch reads as "not generated yet", not as a missing
    feature. Only stretches right of the agent's sealed coverage are reported;
    never-sealed history is omitted (nothing is promised for it).
    """

    model_config = ConfigDict(frozen=True)

    start: datetime
    end: datetime


class RunTimelineBoundaries(BaseModel):
    """Turn rows that anchor the initialized-context-to-compact session."""

    model_config = ConfigDict(frozen=True)

    initialize_turn: int | None
    last_before_compact_turn: int | None
    post_window_turns: int
    has_activity_after_window: bool


RunTimelineMessagePartKind = Literal[
    "think",
    "text",
    "call",
    "out",
    "note",
    "compact",
    "inbound",
    "attach",
    "prompt",
]
RunTimelineMessageKind = Literal[
    "prompt",
    "note",
    "compact",
    "inbound",
    "attach",
    "ai",
    "exec",
]


class RunTimelineMessagePart(BaseModel):
    """One colored part of a strip message; adjacent same-kind parts merge.

    ``chars`` is the part's exact content length — characters as measured
    (the approved width/readout unit), not an estimate.
    """

    model_config = ConfigDict(frozen=True)

    kind: RunTimelineMessagePartKind
    chars: int


class RunTimelineMessage(BaseModel):
    """One context message in the window — the raw strip's geometry source.

    ``key`` is the stable read identity the message-details route resolves
    (current segment ``c.<msg_idx>``; compact history
    ``s<rank>.<boundary>.<msg_idx>``). Message text deliberately stays out of
    this response; the details route serves it on demand.
    """

    model_config = ConfigDict(frozen=True)

    key: str
    idx: int
    ts: datetime | None
    kind: RunTimelineMessageKind
    source: str | None
    chars: int
    parts: list[RunTimelineMessagePart]


class RunTimelineMessageDetailPart(BaseModel):
    """One part's text as the panel reads it."""

    model_config = ConfigDict(frozen=True)

    kind: RunTimelineMessagePartKind
    chars: int
    text: str
    # True when this part's text was clipped by the per-read budget; a
    # `full=true` refetch returns it uncut.
    text_truncated: bool = False


class RunTimelineMessageDetails(BaseModel):
    """GET /api/agents/{agent_id}/run-timeline/message response."""

    model_config = ConfigDict(frozen=True)

    key: str
    kind: RunTimelineMessageKind
    ts: datetime | None
    source: str | None
    chars: int
    parts: list[RunTimelineMessageDetailPart]
    # True when any part came back clipped; refetch with full=true for the
    # uncut text.
    content_truncated: bool


class RunTimelineResponse(BaseModel):
    """GET /api/agents/{agent_id}/run-timeline response."""

    model_config = ConfigDict(frozen=True)

    agent_id: int
    window: RunTimelineWindow
    meta: RunTimelineMeta
    rows: list[RunTimelineRow]
    events: list[RunTimelineEvent]
    boundaries: RunTimelineBoundaries
    # Optional narrative layer — None when no summaries exist for the window.
    layers: list[RunTimelineLayerNode] | None = None
    summary: RunTimelineSummary | None = None
    # De-emphasized placeholders for uncovered window activity -- None when
    # there is nothing to promise (no sealed history, or nothing uncovered).
    # Wire contract: null or a non-empty list; [] is never emitted.
    pending: list[RunTimelinePendingSpan] | None = None
    # Chat delivery facts — None when the read degrades.
    inbounds: list[RunTimelineInbound] | None = None
    # Raw context strip (P4-2, task #4023): one entry per state message in the
    # window, from the same projection the console timeline serves (P4-0
    # direction a). None when the read degrades (the narrative/inbounds
    # posture); the details route serves each message's text on demand.
    messages: list[RunTimelineMessage] | None = None
    # Whether the strip read stopped early — the message budget or the compact
    # history walk cap cut older messages away (never a silent cut; hinted in
    # the UI). None iff ``messages`` is None.
    messages_truncated: bool | None = None
