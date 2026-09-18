"""Wire models for the event-driven agent run timeline."""

from __future__ import annotations

from datetime import datetime

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
