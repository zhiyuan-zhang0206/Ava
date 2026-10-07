"""Understanding endpoints — /api/agents/{id}/sessions and /api/agents/{id}/understanding/*.

The understanding tree is built by the agent host's consumer from chunk jobs: the producers enqueue
them as an agent runs (`base/agents/history/hierarchy/chunks.py`), and this module is the manual way
to build what they did not cover, session by session (a session is the stretch between two
compactions; `base/agents/history/hierarchy/sessions.py`).

- `GET /api/agents/{id}/sessions` lists the sessions with their coverage and an estimated cost;
- `POST /api/agents/{id}/understanding/build` cuts the chosen sessions into chunk jobs (the live
  rule, minus what level 1 already covers) and queues them together with one rebuild of the levels
  above (`base/agents/history/hierarchy/build.py`, `rebuild.py`); `dry_run` plans and prices only;
- `GET /api/agents/{id}/understanding/builds/{build_id}` reports a build's progress and actual cost.

Building the session still in progress is what closing the live segment used to be.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from base.agents.history.checkpoint import (
    CheckpointReadError,
    FullHistory,
    list_compact_boundary_checkpoint_ids,
    load_checkpoint_history_full,
)
from base.agents.history.hierarchy.build import (
    COST_BASIS,
    CostEstimate,
    PlannedJob,
    enqueue_build,
    estimate_cost,
    load_build,
    plan_jobs,
)
from base.agents.history.hierarchy.chunks import chunk_threshold
from base.agents.history.hierarchy.sessions import (
    Session,
    SessionBoundaryError,
    build_sessions,
    coverage_of,
    load_covered_spans,
)
from base.agents.history.hierarchy.units import read_times
from base.agents.observation.snapshot import agent_model_target
from base.config import settings
from base.config.domains.agent.runtime import AgentRuntimeSettings
from base.db import Database, agent_exists
from base.host.env.runtime_config import read_env_aliases
from base.lm.context_budget import UnknownModelWindowError

router = APIRouter()

_ENABLED_ALIAS = "AVA_UNDERSTANDING_ENABLED"
_CHUNK_RATIO_ALIAS = "AVA_UNDERSTANDING_CHUNK_RATIO"


def chunk_ratio() -> float:
    """`AVA_UNDERSTANDING_CHUNK_RATIO`, as the agent host that consumes the jobs reads it.

    The gateway profile does not construct the `agent` config domain (the cluster's value lives in
    the unit's `.env`, which the gateway pops from its environment), so outside a process that has
    the domain the value is read from that file, else the field's default.
    """
    if settings.has_domain("agent"):
        return settings.agent.understanding_chunk_ratio
    raw = read_env_aliases().get(_CHUNK_RATIO_ALIAS)
    if raw:
        return float(raw)
    return AgentRuntimeSettings.model_fields["understanding_chunk_ratio"].default


def feature_enabled() -> bool:
    """`AVA_UNDERSTANDING_ENABLED`, read like `chunk_ratio` (the field's default when unset)."""
    if settings.has_domain("agent"):
        return settings.agent.understanding_enabled
    raw = read_env_aliases().get(_ENABLED_ALIAS)
    if raw is None:
        return AgentRuntimeSettings.model_fields["understanding_enabled"].default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class SessionCoverage(BaseModel):
    """How much of a session's describable material the level-1 nodes cover.

    `none` / `partial` / `full`; `ratio` is `covered_messages / total_messages`. Runs that hold only
    framework notes count as covered. A session with nothing to describe is `full`.
    """

    model_config = ConfigDict(frozen=True)

    status: Literal["none", "partial", "full"]
    ratio: float
    covered_messages: int
    total_messages: int


class CostEstimateOut(BaseModel):
    """The cold-cache price of building what a session (or a request) still lacks.

    `cost_usd` is None when the agent's model has no price. See `cost_basis` of the response.
    """

    model_config = ConfigDict(frozen=True)

    jobs: int
    input_tokens: int
    output_tokens: int
    cost_usd: float | None


class SessionOut(BaseModel):
    """One session. `number` 1 is the oldest. `boundary_checkpoint_id` is None for the session in
    progress. `start` / `end` are the first and last message read times (None when no message has one)."""

    model_config = ConfigDict(frozen=True)

    number: int
    boundary_checkpoint_id: str | None
    start: datetime | None
    end: datetime | None
    messages: int
    peak_input_tokens: int
    coverage: SessionCoverage
    estimate: CostEstimateOut


class SessionsResponse(BaseModel):
    """GET /api/agents/{agent_id}/sessions response.

    `model` is the agent's model, the one the build's jobs run and are priced for. `cost_basis`
    states how every `estimate` is computed.
    """

    model_config = ConfigDict(frozen=True)

    agent_id: int
    model: str
    understanding_enabled: bool
    cost_basis: str
    sessions: list[SessionOut]


class BuildRequest(BaseModel):
    """What to build: `sessions` (numbers from the sessions list) or a `from` / `to` time range
    (every session whose read-time extent intersects it), exactly one of the two."""

    model_config = ConfigDict(populate_by_name=True)

    sessions: list[int] | None = None
    from_: datetime | None = Field(default=None, alias="from")
    to: datetime | None = None
    dry_run: bool = False

    @model_validator(mode="after")
    def _one_selector(self) -> BuildRequest:
        ranged = self.from_ is not None or self.to is not None
        if (self.sessions is not None) == ranged:
            raise ValueError("give either sessions or a from / to range, not both and not neither")
        if self.sessions is not None and not self.sessions:
            raise ValueError("sessions must not be empty")
        for name, value in (("from", self.from_), ("to", self.to)):
            if value is not None and value.tzinfo is None:
                raise ValueError(f"{name} must include a timezone offset")
        return self


class BuildJobOut(BaseModel):
    """One chunk job of a build. `state`: `planned` (a dry run), `enqueued` (new, or an ended job of
    the same stretch taken up again) or `merged` (a pending or running job of the same stretch).
    `start_index` / `end_index` are request-list indices of the session's segment (head at 0);
    `first_message` / `last_message` the inclusive stitched message span the job describes."""

    model_config = ConfigDict(frozen=True)

    session: int
    start_index: int
    end_index: int
    first_message: int
    last_message: int
    input_tokens: int
    job_id: int | None
    state: Literal["planned", "enqueued", "merged"]


class BuildResponse(BaseModel):
    """POST /api/agents/{agent_id}/understanding/build response.

    `build_id` and `rebuild_id` are None for a dry run, which writes nothing. `queued` counts the jobs
    newly on the queue, `merged` those of the same stretch already live. `jobs` may be empty
    (everything chosen is covered): the rebuild of the upper levels is still queued.
    """

    model_config = ConfigDict(frozen=True)

    agent_id: int
    dry_run: bool
    understanding_enabled: bool
    sessions: list[int]
    jobs: list[BuildJobOut]
    queued: int
    merged: int
    estimate: CostEstimateOut
    cost_basis: str
    build_id: int | None
    rebuild_id: int | None


class BuildJobProgress(BaseModel):
    """A build's job as it stands. Token counts and `cost_usd` are summed over its provider calls so
    far (`cost_usd` None when the model has no price)."""

    model_config = ConfigDict(frozen=True)

    job_id: int
    session: int
    status: Literal["pending", "running", "done", "failed", "skipped"]
    start_index: int
    end_index: int
    attempts: int
    error: str | None
    calls: int
    input_tokens: int
    cache_read_tokens: int
    output_tokens: int
    seconds: float
    cost_usd: float | None


class RebuildProgressOut(BaseModel):
    """The build's rebuild of the upper levels. `leaves` is the level-1 nodes it replayed; the usage
    is that of the grouping calls since it started (or, before it starts, since the build); `levels`
    counts the agent's nodes per level above 1 right now."""

    model_config = ConfigDict(frozen=True)

    id: int
    status: Literal["pending", "running", "done", "failed"]
    attempts: int
    error: str | None
    leaves: int
    calls: int
    input_tokens: int
    cache_read_tokens: int
    output_tokens: int
    seconds: float
    cost_usd: float | None
    levels: dict[int, int]


class BuildProgressResponse(BaseModel):
    """GET /api/agents/{agent_id}/understanding/builds/{build_id} response.

    `phase`: `chunks` (jobs still live), `rebuild_pending` (jobs ended; the rebuild waits for the
    agent's chunk jobs to end or is running), `done`, `failed` (the rebuild failed). A job that failed
    leaves its stretch undescribed; the build still completes. `cost_usd` is the actual total so far.
    """

    model_config = ConfigDict(frozen=True)

    build_id: int
    agent_id: int
    created_at: datetime
    sessions: list[int]
    phase: Literal["chunks", "rebuild_pending", "done", "failed"]
    jobs: list[BuildJobProgress]
    rebuild: RebuildProgressOut
    cost_usd: float | None


def _estimate_out(estimate: CostEstimate) -> CostEstimateOut:
    return CostEstimateOut(**asdict(estimate))


def _job_out(
    job: PlannedJob, job_id: int | None, state: Literal["planned", "enqueued", "merged"]
) -> BuildJobOut:
    return BuildJobOut(
        session=job.session,
        start_index=job.start_index,
        end_index=job.end_index,
        first_message=job.first_message,
        last_message=job.last_message,
        input_tokens=job.input_tokens,
        job_id=job_id,
        state=state,
    )


def _require_agent(request: Request, agent_id: int) -> None:
    with request.app.state.db_pool.connection() as conn:
        if not agent_exists(conn, agent_id):
            raise HTTPException(status_code=404, detail=f"agent {agent_id} not found")


def _load(db: Database, agent_id: int) -> tuple[FullHistory, list[Session]]:
    """The stored history and its sessions (blocking)."""
    history = load_checkpoint_history_full(db, agent_id)
    boundaries = list(reversed(list_compact_boundary_checkpoint_ids(db, agent_id)))
    return history, build_sessions(history, boundaries, read_times(history.messages))


def build_model(db: Database, agent_id: int) -> str:
    return agent_model_target(db, agent_id, fallback=settings.lm.hierarchy_model)[0]


def chunk_size(db: Database, agent_id: int) -> int:
    """The agent's chunk size in tokens: the ratio of its own model's soft compaction threshold,
    with its own overrides (the rule of the live hook)."""
    model, overrides = agent_model_target(db, agent_id, fallback=settings.lm.hierarchy_model)
    return chunk_threshold(model, overrides, chunk_ratio())


def _sessions_blocking(request: Request, agent_id: int) -> SessionsResponse:
    _require_agent(request, agent_id)
    db: Database = request.app.state.db
    history, sessions = _load(db, agent_id)
    covered = load_covered_spans(request.app.state.db_pool, agent_id)
    model = build_model(db, agent_id)
    jobs = plan_jobs(history, sessions, covered, threshold=chunk_size(db, agent_id))
    return SessionsResponse(
        agent_id=agent_id,
        model=model,
        understanding_enabled=feature_enabled(),
        cost_basis=COST_BASIS,
        sessions=[
            SessionOut(
                number=s.number,
                boundary_checkpoint_id=s.boundary_checkpoint_id,
                start=s.start,
                end=s.ended,
                messages=s.messages,
                peak_input_tokens=s.peak_input_tokens,
                coverage=SessionCoverage(**asdict(coverage_of(history, s, covered))),
                estimate=_estimate_out(
                    estimate_cost(model, [j for j in jobs if j.session == s.number])
                ),
            )
            for s in sessions
        ],
    )


def _select_numbers(sessions: list[Session], wanted: list[int]) -> list[Session]:
    known = {s.number: s for s in sessions}
    unknown = sorted({n for n in wanted if n not in known})
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"unknown session numbers {unknown}; the agent has sessions 1..{len(sessions)}",
        )
    return [known[n] for n in sorted(set(wanted))]


def _select_range(
    sessions: list[Session], from_: datetime | None, to: datetime | None
) -> list[Session]:
    chosen = [
        s
        for s in sessions
        if s.start is not None
        and s.ended is not None
        and (to is None or s.start <= to)
        and (from_ is None or s.ended >= from_)
    ]
    if not chosen:
        raise HTTPException(status_code=422, detail="no session intersects the time range")
    return chosen


def _select(sessions: list[Session], body: BuildRequest) -> list[Session]:
    if body.sessions is not None:
        return _select_numbers(sessions, body.sessions)
    return _select_range(sessions, body.from_, body.to)


def _build_blocking(request: Request, agent_id: int, body: BuildRequest) -> BuildResponse:
    _require_agent(request, agent_id)
    db: Database = request.app.state.db
    history, sessions = _load(db, agent_id)
    chosen = _select(sessions, body)
    covered = load_covered_spans(request.app.state.db_pool, agent_id)
    planned = plan_jobs(history, chosen, covered, threshold=chunk_size(db, agent_id))
    estimate = _estimate_out(estimate_cost(build_model(db, agent_id), planned))
    numbers = [s.number for s in chosen]
    enabled = feature_enabled()
    if body.dry_run:
        return BuildResponse(
            agent_id=agent_id,
            dry_run=True,
            understanding_enabled=enabled,
            sessions=numbers,
            jobs=[_job_out(j, None, "planned") for j in planned],
            queued=0,
            merged=0,
            estimate=estimate,
            cost_basis=COST_BASIS,
            build_id=None,
            rebuild_id=None,
        )
    build = enqueue_build(request.app.state.db_pool, agent_id, numbers, planned)
    return BuildResponse(
        agent_id=agent_id,
        dry_run=False,
        understanding_enabled=enabled,
        sessions=numbers,
        jobs=[_job_out(e.planned, e.job_id, e.state) for e in build.jobs],
        queued=sum(e.state == "enqueued" for e in build.jobs),
        merged=sum(e.state == "merged" for e in build.jobs),
        estimate=estimate,
        cost_basis=COST_BASIS,
        build_id=build.id,
        rebuild_id=build.rebuild_id,
    )


async def _run[T](fn: Callable[..., T], *args: object) -> T:
    try:
        return await asyncio.to_thread(fn, *args)
    except CheckpointReadError as exc:
        raise HTTPException(status_code=503, detail=f"history unreadable: {exc}") from exc
    except UnknownModelWindowError as exc:
        raise HTTPException(status_code=409, detail=f"chunk size unknown: {exc}") from exc
    except SessionBoundaryError as exc:
        raise HTTPException(
            status_code=409, detail=f"history segments and boundaries disagree: {exc}"
        ) from exc


@router.get("/api/agents/{agent_id}/sessions")
async def get_agent_sessions(agent_id: int, request: Request) -> SessionsResponse:
    """The agent's sessions (the stretches between two compactions), oldest first, with their coverage by the understanding tree and what building the rest would cost.

    Numbers are 1-based, grow with time and are stable: a new session is only ever added at the
    end. The last session has no boundary checkpoint while it is still in progress. Times are the
    messages' read times; `peak_input_tokens` is the largest provider-reported input of any
    request in the session. `coverage` counts the session's describable material (past its
    framework head, up to the last request the agent sent) that level-1 nodes already describe;
    `estimate` prices building what is still missing, on a cold cache and at full input price (see
    `cost_basis`: it is an estimate from the stored history, not a quote). 404 when the agent does
    not exist; 503 when the stored history cannot be read.
    """
    return await _run(_sessions_blocking, request, agent_id)


@router.post("/api/agents/{agent_id}/understanding/build")
async def post_understanding_build(
    agent_id: int, body: BuildRequest, request: Request
) -> BuildResponse:
    """Build the understanding tree of chosen sessions by hand, then rebuild the levels above.

    Choose sessions by number (`sessions`) or by a `from` / `to` range (every session whose
    read-time extent intersects it). Each chosen session is cut into chunks by the live rule
    (a chunk per `AVA_UNDERSTANDING_CHUNK_RATIO` x the agent model's soft compaction threshold of
    growth in the provider-reported input, the session's remainder last); what level-1 nodes already cover is skipped exactly, a chunk that
    overlaps it is cut to its uncovered runs. The jobs run on the agent hosts' consumer in order,
    one agent at a time; the session still in progress is built to the last request the agent has
    sent. Once the agent's chunk jobs have all ended, every node above level 1 and the grouping
    cursor are dropped and the levels above are rebuilt over all of the agent's level-1 nodes in
    message order; builds of one agent in flight merge into one rebuild. A job that fails leaves
    its stretch undescribed and the rebuild runs anyway.

    Cost warning: these calls are NOT warm. A manual build reads a stored history long after the
    agent's last request, so every job's whole conversation prefix is billed at the full
    (cache-miss) input price; a 390K-token session costs that much input once per chunk. Always run
    with `dry_run=true` first: it returns the chunk jobs that would be queued and the estimated
    cost (`cost_basis` says how) without writing anything, and it works whatever the feature switch.

    The feature switch: with `AVA_UNDERSTANDING_ENABLED` off the build is still queued (jobs and
    rebuild) and waits, undescribed, until the switch is turned on; `understanding_enabled` in the
    response says which it is. A repeat request merges into the jobs and the pending rebuild already
    queued and only records another build. Returns the jobs queued and a `build_id` for
    `GET .../understanding/builds/{build_id}`. 404 when the agent does not exist; 422 for an
    unknown session number, an empty selection or a range no session intersects; 503 when the
    stored history cannot be read.
    """
    return await _run(_build_blocking, request, agent_id, body)


def _progress_blocking(request: Request, agent_id: int, build_id: int) -> BuildProgressResponse:
    _require_agent(request, agent_id)
    progress = load_build(request.app.state.db_pool, agent_id, build_id)
    if progress is None:
        raise HTTPException(
            status_code=404, detail=f"build {build_id} of agent {agent_id} not found"
        )
    return BuildProgressResponse.model_validate(asdict(progress))


@router.get("/api/agents/{agent_id}/understanding/builds/{build_id}")
async def get_understanding_build(
    agent_id: int, build_id: int, request: Request
) -> BuildProgressResponse:
    """A build's progress: each job's status and actual cost, then the rebuild of the upper levels.

    `phase` runs `chunks` (jobs live) then `rebuild_pending` (the rebuild waits for the agent's
    chunk jobs to end or is running) then `done`, or `failed` when the rebuild failed. Costs are
    summed from the raw record of the provider calls so far at the price book's rates; a stalled
    build with `AVA_UNDERSTANDING_ENABLED` off stays in `chunks` with its jobs `pending`. 404 when
    the agent or the build (of this agent) does not exist.
    """
    return await _run(_progress_blocking, request, agent_id, build_id)
