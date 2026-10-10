"""Manual build of the understanding tree, session by session.

A build cuts the chosen sessions' material (`sessions.py`) into chunks with the live trigger rule
(`chunk_plan.plan_replay`, threshold `chunks.chunk_threshold`: a chunk per `AVA_UNDERSTANDING_CHUNK_RATIO` x the agent model's
soft compaction threshold of growth in the provider-reported input, the session's closing remainder last), subtracts what
level-1 nodes already cover — a chunk is cut down to the runs nothing covers, one job per run — and
enqueues the rest as ordinary chunk jobs. The consumer describes them like any other; the levels
above are rebuilt once the agent's chunk jobs have all ended (`rebuild.py`). A build records its jobs
and its rebuild (`understanding_builds`) so its progress can be read (`load_build`).

Chunks are cut and priced from the stored history alone; nothing is written by `plan_jobs` or
`estimate_cost`, which is what a dry run is.

Cost estimate (the basis, stated once so every figure reads the same):

- Input: a job's call sends the agent's own conversation up to the chunk's end plus an instruction.
  The prefix is a manual build's, read from the stored checkpoint long after the agent's last
  request, so the provider cache is cold: the whole prefix is billed at the full cache-miss input
  rate. Its size is the provider-reported `input_tokens` of the agent's request that follows the
  chunk (or, past the last request, that request's input plus its output).
- Rounds: measured on the preview cluster (116 jobs), a job makes 1.85 calls on average — the reply
  and, now and then, a correction of the groups. The extra calls re-read the same prefix from the
  cache, billed at the cache-hit rate.
- Output: 8,800 tokens per job (reasoning included, billed at the output rate), the same measurement.
- Rates: the repository's price book (`base.lm.pricing.quote`) for the agent's own model, which is
  the model the job runs; `None` when that model is not priced.
- Not included: the grouping calls of the levels above (small next to the chunk calls: they read
  summaries, not conversation).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from langchain_core.messages import AIMessage, AnyMessage
from psycopg_pool import ConnectionPool

from base.agents.history.checkpoint import FullHistory
from base.agents.history.hierarchy.chunk_plan import plan_replay, segment_requests
from base.agents.history.hierarchy.chunks import uncovered
from base.agents.history.hierarchy.sessions import Session, has_matter
from base.agents.history.timeline_inputs import TimelineReadInputs
from base.db.transaction import write_transaction
from base.lm.pricing import PriceBook, quote

# Measured on the preview cluster's chunk calls (see the module docstring).
CALLS_PER_JOB = 1.85
OUTPUT_TOKENS_PER_JOB = 8800

COST_BASIS = (
    "Cold cache: each job's whole conversation prefix is billed at the full cache-miss input rate "
    "(a manual build reads a stored history, not a warm one). Rounds beyond the first re-read the "
    "prefix at the cache-hit rate (1.85 calls per job on average); output is 8,800 tokens per job "
    "(reasoning included), both measured on the preview cluster. Rates are the price book's for "
    "the agent's model; the grouping calls of the levels above are not included."
)


@dataclass(frozen=True)
class PlannedJob:
    """One chunk job a build would enqueue: request-list indices of its session's segment."""

    session: int
    compact_version: int
    start_index: int
    end_index: int
    end_msg_id: str
    boundary_checkpoint_id: str | None
    first_message: int
    last_message: int
    input_tokens: int


@dataclass(frozen=True)
class CostEstimate:
    """What a set of jobs would cost on a cold cache (the basis: `COST_BASIS`)."""

    jobs: int
    input_tokens: int
    output_tokens: int
    cost_usd: float | None


def _turn_tokens(request: Sequence[AnyMessage]) -> list[tuple[int, int, int]]:
    """`(index, input_tokens, output_tokens)` of every AIMessage with usage, in order."""
    return [
        (i, int(msg.usage_metadata["input_tokens"]), int(msg.usage_metadata["output_tokens"]))
        for i, msg in enumerate(request)
        if isinstance(msg, AIMessage) and msg.usage_metadata
    ]


def _prefix_tokens(turns: Sequence[tuple[int, int, int]], end_index: int) -> int:
    """The size of the request up to `end_index` (exclusive): the provider-reported input of the
    turn at `end_index`, else of the last turn before it plus that turn's output; 0 with no turn."""
    before: tuple[int, int, int] | None = None
    for turn in turns:
        if turn[0] == end_index:
            return turn[1]
        if turn[0] > end_index:
            break
        before = turn
    return 0 if before is None else before[1] + before[2]


def plan_jobs(
    history: FullHistory,
    sessions: Sequence[Session],
    covered: Sequence[tuple[int, int]],
    *,
    threshold: int,
    timeline_inputs: TimelineReadInputs,
) -> list[PlannedJob]:
    """The jobs that describe what the chosen sessions' level-1 nodes do not yet cover, oldest first.

    `covered` is the sorted spans of the agent's level-1 nodes (`sessions.load_covered_spans`). A
    chunk of the live rule that overlaps them is cut to its uncovered runs (one job each, runs with
    only framework notes dropped); a closed session's jobs name its boundary checkpoint.
    """
    requests = segment_requests(history)
    jobs: list[PlannedJob] = []
    for session in sessions:
        request = requests[session.segment]
        offset = 1 if history.segment_heads[session.segment] is not None else 0
        base = session.first - offset  # stitched index of request index 0
        turns = _turn_tokens(request)
        for planned in plan_replay(request, threshold=threshold, close=True):
            span = (base + planned.chunk.start_index, base + planned.chunk.end_index - 1)
            for first, last in uncovered(span, covered):
                if not has_matter(
                    history.messages[first : last + 1], timeline_inputs=timeline_inputs
                ):
                    continue
                end_index = last - base + 1
                end_msg_id = request[end_index - 1].id
                if end_msg_id is None:
                    raise ValueError(
                        f"session {session.number}: message {last} has no id, so a job ending there could not be located"
                    )
                jobs.append(
                    PlannedJob(
                        session=session.number,
                        compact_version=session.segment,
                        start_index=first - base,
                        end_index=end_index,
                        end_msg_id=end_msg_id,
                        boundary_checkpoint_id=session.boundary_checkpoint_id,
                        first_message=first,
                        last_message=last,
                        input_tokens=_prefix_tokens(turns, end_index),
                    )
                )
    return jobs


def estimate_cost(model: str, jobs: Sequence[PlannedJob], *, prices: PriceBook) -> CostEstimate:
    """The cold-cache price of `jobs` for `model` (`COST_BASIS`); `cost_usd` None when it is unpriced
    or a job's prefix size is unknown."""
    total: float | None = 0.0
    for job in jobs:
        prefix = job.input_tokens
        first = (
            quote(model, prefix, OUTPUT_TOKENS_PER_JOB, 0, prices=prices) if prefix > 0 else None
        )
        again = quote(model, prefix, 0, prefix, prices=prices) if prefix > 0 else None
        if first is None or again is None or total is None:
            total = None
        else:
            total += first.cost_usd + (CALLS_PER_JOB - 1) * again.cost_usd
    return CostEstimate(
        jobs=len(jobs),
        input_tokens=sum(job.input_tokens for job in jobs),
        output_tokens=OUTPUT_TOKENS_PER_JOB * len(jobs),
        cost_usd=total,
    )


# A job of the same identity that ended (failed, skipped, or done with its stretch still
# uncovered) is taken up again; one pending or running is merged into. `prior` is its status before.
_ENQUEUE_SQL = """
WITH prior AS (
    SELECT status FROM understanding_chunk_jobs
    WHERE agent_id = %(agent)s AND compact_version = %(version)s
      AND start_index = %(start)s AND end_index = %(end)s
)
INSERT INTO understanding_chunk_jobs AS j
    (agent_id, compact_version, start_index, end_index, end_msg_id, boundary_checkpoint_id)
VALUES (%(agent)s, %(version)s, %(start)s, %(end)s, %(end_msg_id)s, %(boundary)s)
ON CONFLICT (agent_id, compact_version, start_index, end_index) DO UPDATE SET
    status = CASE WHEN j.status IN __ENDED__ THEN 'pending' ELSE j.status END,
    attempts = CASE WHEN j.status IN __ENDED__ THEN 0 ELSE j.attempts END,
    error = CASE WHEN j.status IN __ENDED__ THEN NULL ELSE j.error END,
    claimed_at = CASE WHEN j.status IN __ENDED__ THEN NULL ELSE j.claimed_at END,
    waiting_since = CASE WHEN j.status IN __ENDED__ THEN NULL ELSE j.waiting_since END,
    finished_at = CASE WHEN j.status IN __ENDED__ THEN NULL ELSE j.finished_at END,
    end_msg_id = CASE WHEN j.status IN __ENDED__ THEN EXCLUDED.end_msg_id ELSE j.end_msg_id END,
    boundary_checkpoint_id = CASE WHEN j.status IN __ENDED__
        THEN EXCLUDED.boundary_checkpoint_id ELSE j.boundary_checkpoint_id END
RETURNING j.id, (SELECT status FROM prior)
""".replace("__ENDED__", "('failed', 'skipped', 'done')")

JobState = Literal["enqueued", "merged"]


@dataclass(frozen=True)
class EnqueuedJob:
    """A planned job as it stands on the queue: new or revived (`enqueued`), or a live job of the
    same stretch it merged into (`merged`)."""

    job_id: int
    state: JobState
    planned: PlannedJob


@dataclass(frozen=True)
class Build:
    """One recorded build."""

    id: int
    agent_id: int
    rebuild_id: int
    jobs: list[EnqueuedJob]


def enqueue_build(
    pool: ConnectionPool, agent_id: int, sessions: Sequence[int], jobs: Sequence[PlannedJob]
) -> Build:
    """Enqueue `jobs`, join the agent's pending upper-level rebuild (or add one) and record the build.

    One transaction under the agent's build lock: builds of one agent are serialized, and the
    pending rebuild row stays locked until the commit, so the claimer cannot take it between the
    jobs and the rebuild (`rebuild.py`).
    """
    with write_transaction(pool) as conn:
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"understanding-build:{agent_id}",),
        )
        enqueued: list[EnqueuedJob] = []
        for job in jobs:
            row = conn.execute(
                _ENQUEUE_SQL,
                {
                    "agent": agent_id,
                    "version": job.compact_version,
                    "start": job.start_index,
                    "end": job.end_index,
                    "end_msg_id": job.end_msg_id,
                    "boundary": job.boundary_checkpoint_id,
                },
            ).fetchone()
            assert row is not None, "an upsert with RETURNING always returns a row"  # noqa: S101
            live = row[1] in ("pending", "running")
            enqueued.append(EnqueuedJob(int(row[0]), "merged" if live else "enqueued", job))
        pending = conn.execute(
            "SELECT id FROM understanding_rebuilds WHERE agent_id = %s AND status = 'pending'"
            " ORDER BY id LIMIT 1 FOR UPDATE",
            (agent_id,),
        ).fetchone()
        if pending is None:
            pending = conn.execute(
                "INSERT INTO understanding_rebuilds (agent_id) VALUES (%s) RETURNING id",
                (agent_id,),
            ).fetchone()
        assert pending is not None, "an insert with RETURNING always returns a row"  # noqa: S101
        rebuild_id = int(pending[0])
        manifest = [{"job_id": e.job_id, "session": e.planned.session} for e in enqueued]
        row = conn.execute(
            "INSERT INTO understanding_builds (agent_id, sessions, jobs, rebuild_id)"
            " VALUES (%s, %s, %s::jsonb, %s) RETURNING id",
            (agent_id, list(sessions), json.dumps(manifest), rebuild_id),
        ).fetchone()
        assert row is not None, "an insert with RETURNING always returns a row"  # noqa: S101
    return Build(int(row[0]), agent_id, rebuild_id, enqueued)


@dataclass(frozen=True)
class JobProgress:
    """One of a build's jobs as it stands, with what its provider calls cost so far."""

    job_id: int
    session: int
    status: str
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


@dataclass(frozen=True)
class RebuildProgress:
    """The build's upper-level rebuild: its status, the leaves it replayed, and the grouping calls
    its latest run made."""

    id: int
    status: str
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


@dataclass(frozen=True)
class BuildProgress:
    """A build's progress. `phase`: `chunks` (jobs still live), `rebuild_pending` (jobs ended, the
    rebuild is waiting or running), `done`, or `failed` (the rebuild failed)."""

    build_id: int
    agent_id: int
    created_at: datetime
    sessions: list[int]
    phase: Literal["chunks", "rebuild_pending", "done", "failed"]
    jobs: list[JobProgress]
    rebuild: RebuildProgress
    cost_usd: float | None


def _price(
    model: str, tok_in: int, tok_out: int, cached: int, *, prices: PriceBook
) -> float | None:
    priced = quote(model, tok_in, tok_out, min(cached, tok_in), prices=prices)
    return None if priced is None else priced.cost_usd


def _sum_costs(costs: Sequence[float | None]) -> float | None:
    return None if any(c is None for c in costs) else float(sum(c for c in costs if c is not None))


@dataclass(frozen=True)
class _BuildRows:
    """The raw rows of one build (`_read_build`)."""

    created_at: datetime
    sessions: list[int]
    by_job: dict[int, int]
    jobs: list[tuple[Any, ...]]
    calls: list[tuple[Any, ...]]
    rebuild_id: int
    rebuild: tuple[Any, ...]
    group_calls: list[tuple[Any, ...]]
    levels: dict[int, int]


def _read_build(pool: ConnectionPool, agent_id: int, build_id: int) -> _BuildRows | None:
    with pool.connection() as conn:
        build = conn.execute(
            "SELECT created_at, sessions, jobs, rebuild_id FROM understanding_builds"
            " WHERE id = %s AND agent_id = %s",
            (build_id, agent_id),
        ).fetchone()
        if build is None:
            return None
        created_at, sessions, manifest, rebuild_id = build
        by_job = {int(m["job_id"]): int(m["session"]) for m in manifest}
        jobs = conn.execute(
            "SELECT id, status, start_index, end_index, attempts, error"
            " FROM understanding_chunk_jobs WHERE id = ANY(%s) ORDER BY id",
            (list(by_job),),
        ).fetchall()
        calls = conn.execute(
            "SELECT job_id, model, coalesce((usage_metadata->>'input_tokens')::int, 0),"
            " coalesce((usage_metadata->'input_token_details'->>'cache_read')::int, 0),"
            " coalesce((usage_metadata->>'output_tokens')::int, 0), duration_ms / 1000.0"
            " FROM understanding_chunk_calls WHERE job_id = ANY(%s)",
            (list(by_job),),
        ).fetchall()
        rebuild = conn.execute(
            "SELECT status, attempts, error, leaves, claimed_at, finished_at"
            " FROM understanding_rebuilds WHERE id = %s",
            (rebuild_id,),
        ).fetchone()
        assert rebuild is not None, "a build's rebuild row is never deleted"  # noqa: S101
        group_calls = conn.execute(
            "SELECT model, coalesce((usage_metadata->>'input_tokens')::int, 0),"
            " coalesce((usage_metadata->'input_token_details'->>'cache_read')::int, 0),"
            " coalesce((usage_metadata->>'output_tokens')::int, 0), duration_ms / 1000.0"
            " FROM understanding_group_calls WHERE agent_id = %s AND created_at >= %s"
            " AND (%s::timestamptz IS NULL OR created_at <= %s)",
            (agent_id, rebuild[4] or created_at, rebuild[5], rebuild[5]),
        ).fetchall()
        levels = conn.execute(
            "SELECT depth, count(*) FROM understanding_nodes WHERE agent_id = %s AND depth > 1"
            " GROUP BY depth ORDER BY depth",
            (agent_id,),
        ).fetchall()
    return _BuildRows(
        created_at=created_at,
        sessions=[int(n) for n in sessions],
        by_job=by_job,
        jobs=jobs,
        calls=calls,
        rebuild_id=int(rebuild_id),
        rebuild=rebuild,
        group_calls=group_calls,
        levels={int(depth): int(count) for depth, count in levels},
    )


def _rebuild_progress(rows: _BuildRows, *, prices: PriceBook) -> RebuildProgress:
    status, attempts, error, leaves = rows.rebuild[:4]
    group = rows.group_calls
    return RebuildProgress(
        id=rows.rebuild_id,
        status=str(status),
        attempts=int(attempts),
        error=None if error is None else str(error),
        leaves=int(leaves or 0),
        calls=len(group),
        input_tokens=sum(int(r[1]) for r in group),
        cache_read_tokens=sum(int(r[2]) for r in group),
        output_tokens=sum(int(r[3]) for r in group),
        seconds=float(sum(float(r[4]) for r in group)),
        cost_usd=_sum_costs(
            [_price(str(r[0]), int(r[1]), int(r[3]), int(r[2]), prices=prices) for r in group]
        ),
        levels=rows.levels,
    )


def _phase(
    jobs: Sequence[JobProgress], rebuild_status: str
) -> Literal["chunks", "rebuild_pending", "done", "failed"]:
    if any(job.status in ("pending", "running") for job in jobs):
        return "chunks"
    if rebuild_status in ("done", "failed"):
        return rebuild_status
    return "rebuild_pending"


def load_build(
    pool: ConnectionPool, agent_id: int, build_id: int, *, prices: PriceBook
) -> BuildProgress | None:
    """The build's progress, or None when no such build of this agent exists."""
    rows = _read_build(pool, agent_id, build_id)
    if rows is None:
        return None
    jobs = [
        _job_progress(row, rows.by_job[int(row[0])], rows.calls, prices=prices) for row in rows.jobs
    ]
    rebuild = _rebuild_progress(rows, prices=prices)
    return BuildProgress(
        build_id=build_id,
        agent_id=agent_id,
        created_at=rows.created_at,
        sessions=rows.sessions,
        phase=_phase(jobs, rebuild.status),
        jobs=jobs,
        rebuild=rebuild,
        cost_usd=_sum_costs([*(j.cost_usd for j in jobs), rebuild.cost_usd]),
    )


def _job_progress(
    row: tuple[Any, ...],
    session: int,
    calls: Sequence[tuple[Any, ...]],
    *,
    prices: PriceBook,
) -> JobProgress:
    mine = [c for c in calls if int(c[0]) == int(row[0])]
    return JobProgress(
        job_id=int(row[0]),
        session=session,
        status=str(row[1]),
        start_index=int(row[2]),
        end_index=int(row[3]),
        attempts=int(row[4]),
        error=None if row[5] is None else str(row[5]),
        calls=len(mine),
        input_tokens=sum(int(c[2]) for c in mine),
        cache_read_tokens=sum(int(c[3]) for c in mine),
        output_tokens=sum(int(c[4]) for c in mine),
        seconds=float(sum(float(c[5]) for c in mine)),
        cost_usd=_sum_costs(
            [_price(str(c[1]), int(c[2]), int(c[4]), int(c[3]), prices=prices) for c in mine]
        ),
    )
