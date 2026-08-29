"""Run timeline — run→turn→call 3-level visualization data.

The run-timeline endpoint is the read-side aggregation for the run-level
waterfall page: one agent's event river (Loki `llm_usage` / `turn_end` /
`exec*` / `compact` / lifecycle) reassembled into the three hierarchy levels
the visualization renders. The data surface is reused as-is (zero new
collection — the design doc's data plane is already complete).

Level semantics:
- **run** = one agent's whole window (default: the session route from
  `initialize` to `compact`, adjustable via `from`/`to`); `meta` carries the
  window aggregates and `boundaries` the session-route markers.
- **turn** = one turn (`llm_usage` joined 1:1 to `turn_end` by `span_id`);
  `rows[].llm` holds the call's absolute token counts and cost.
- **call** = the LLM call plus every exec inside the turn, embedded per row
  (`llm` + `execs`); the frontend expands a turn row into its calls.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt


class RunTimelineLlm(BaseModel):
    """One turn's LLM call (events side; spans carry the same numbers).

    `in_total` is the absolute input-token count of the call — the token-axis
    value the visualization shows (user ruling: token itself, absolute
    quantity; no cache/input/output split on the axis — input vs output may
    be color-differentiated instead). `cost_usd` is the usage-time price
    snapshot; `model` the model in force at the call."""

    model_config = ConfigDict(frozen=True)

    calls: NonNegativeInt
    in_total: NonNegativeInt
    cache_read: NonNegativeInt
    out_total: NonNegativeInt
    reasoning: NonNegativeInt
    latency_ms: float = Field(ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
    model: str


class RunTimelineExec(BaseModel):
    """One exec inside a turn — a call-level leaf."""

    model_config = ConfigDict(frozen=True)

    tool: str
    ok: bool


class RunTimelineRow(BaseModel):
    """One turn row. `start`/`end` are wall clock; `active_s` is the turn's
    own duration (`turn_end.duration_seconds`). `anomalies` lists
    human-readable markers (exec_failed / exec_timeout / llm_turn_aborted …)
    for the red-highlight pass; `tags` carries lifecycle markers
    (compact / restart / long-idle) for the event rail."""

    model_config = ConfigDict(frozen=True)

    turn: NonNegativeInt
    start: datetime
    end: datetime
    active_s: float = Field(ge=0)
    ok: bool
    trace_id: str
    llm: RunTimelineLlm | None
    execs: list[RunTimelineExec]
    anomalies: list[str]
    tags: list[str]


class RunTimelineBoundaries(BaseModel):
    """The session route the window resolved to. `initialize_at` is the
    context-initialization instant (the first turn's start inside the
    window); `compact_at` is the latest compact event inside the window
    (None when the session has not compacted yet). Default window =
    `[initialize_at, compact_at]` (or now when `compact_at` is None)."""

    model_config = ConfigDict(frozen=True)

    initialize_at: datetime | None
    compact_at: datetime | None


class RunTimelineMeta(BaseModel):
    """Window aggregates — the run-level header stats. `truncated` is true
    when the turn fetch hit the row cap (a very long window); the caller can
    narrow the window for the full picture."""

    model_config = ConfigDict(frozen=True)

    n_turns: NonNegativeInt
    wall_span_s: float = Field(ge=0)
    active_s: float = Field(ge=0)
    tokens_in: NonNegativeInt
    tokens_out: NonNegativeInt
    cost_usd: float = Field(ge=0)
    n_exec_failed: NonNegativeInt
    n_compact: NonNegativeInt
    n_restart: NonNegativeInt
    truncated: bool = False
    warnings: list[str] = Field(default_factory=list)


class RunTimelineResponse(BaseModel):
    """GET /api/agents/{id}/run-timeline response."""

    model_config = ConfigDict(frozen=True)

    agent_id: int
    window_from: datetime
    window_to: datetime
    boundaries: RunTimelineBoundaries
    meta: RunTimelineMeta
    rows: list[RunTimelineRow]
