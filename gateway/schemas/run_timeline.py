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
    """One execution event attached to the containing turn."""

    model_config = ConfigDict(frozen=True)

    tool: str
    dur_s: float
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


class RunTimelineBoundaries(BaseModel):
    """Turn rows that anchor the initialized-context-to-compact session."""

    model_config = ConfigDict(frozen=True)

    initialize_turn: int | None
    last_before_compact_turn: int | None


class RunTimelineResponse(BaseModel):
    """GET /api/agents/{agent_id}/run-timeline response."""

    model_config = ConfigDict(frozen=True)

    agent_id: int
    window: RunTimelineWindow
    meta: RunTimelineMeta
    rows: list[RunTimelineRow]
    events: list[RunTimelineEvent]
    boundaries: RunTimelineBoundaries
