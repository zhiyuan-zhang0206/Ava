"""Understanding endpoints — /api/agents/{id}/understanding/*.

The understanding tree is built by the agent host's consumer from chunk jobs the producers enqueue
as an agent runs (`base/agents/history/hierarchy/chunks.py`). The one operator action is closing
an agent's live segment by hand: the part no chunk has described yet.

The producers of `chunks.py` cover a segment as it grows (size cuts) and when it is compacted
(the closing chunk). An agent that never reaches the first cut, or ends before it compacts,
leaves a tail nobody describes; this module enqueues that tail on demand
(`POST /api/agents/{id}/understanding/close`, below).

`close_segment` plans one job from the stored state alone, the way the producers would have:

- the segment is the newest of the checkpoint history; its jobs are recognised by message id (the
  request holds a job's `end_msg_id` where it recorded it), and the job's `compact_version` is
  reused, so a boundary stamp that failed once does not make the close a segment of its own;
- the chunk starts where the last job of that segment left off — that job's end, else the
  segment's first message past its head (a failed job's stretch is taken up again, a skipped
  one's is not) — and never before the end of a level-1 node the segment already has, so the
  nodes of one level cannot overlap; it ends at the last request the agent sent (`sendable_len`);
- the consumer reads it from the live checkpoint rather than a compaction boundary.

The call answers with a status and writes nothing unless it is `enqueued`: `active_job` (a job of the agent is pending or running — the queue keeps one
agent's jobs in order, and a second close would race the first), `empty` (nothing past the
start).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from langchain_core.messages import BaseMessage
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from base.agents.history.checkpoint import CheckpointReadError, load_checkpoint_history_full
from base.agents.history.hierarchy.chunks import segment_head_len, sendable_len
from base.db import Database, agent_exists
from base.db.transaction import write_transaction

router = APIRouter()

CloseStatus = Literal["enqueued", "empty", "active_job"]

# A failed or skipped job of the same identity is retried; a pending, running or done one stays.
_ENQUEUE_SQL = """
INSERT INTO understanding_chunk_jobs
    (agent_id, compact_version, start_index, end_index, end_msg_id)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (agent_id, compact_version, start_index, end_index) DO UPDATE SET
    status = 'pending', attempts = 0, error = NULL, claimed_at = NULL, waiting_since = NULL,
    finished_at = NULL,
    end_msg_id = EXCLUDED.end_msg_id
WHERE understanding_chunk_jobs.status IN ('failed', 'skipped')
RETURNING id
"""


@dataclass(frozen=True)
class CloseResult:
    """What a close request did: its status, the job it enqueued or found, and the stretch."""

    status: CloseStatus
    job_id: int | None = None
    compact_version: int | None = None
    start_index: int | None = None
    end_index: int | None = None
    detail: str = ""


def _active_job(pool: ConnectionPool, agent_id: int) -> tuple[int, str] | None:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT id, status FROM understanding_chunk_jobs"
            " WHERE agent_id = %s AND status IN ('pending', 'running') ORDER BY id LIMIT 1",
            (agent_id,),
        ).fetchone()
    return None if row is None else (int(row[0]), str(row[1]))


def _live_segment_state(
    pool: ConnectionPool, agent_id: int, request: list[BaseMessage], base: int
) -> tuple[int | None, int | None, int]:
    """`(version, start, covered_to)` of the live segment from what is stored about it.

    The segment's jobs are recognised by message id (a job belongs to the live segment when
    the request holds its `end_msg_id` at the position it recorded), not by a version number, so
    one failed boundary stamp cannot put the close in a segment of its own. `version` and `start`
    come from the newest such job (None, None when there is none: the segment has no job yet);
    `covered_to` is the first request index past the last level-1 node the segment already has
    (`base` converts request positions to stitched message indices).
    """
    with pool.connection() as conn:
        jobs = conn.execute(
            "SELECT compact_version, status, start_index, end_index, end_msg_id"
            " FROM understanding_chunk_jobs WHERE agent_id = %s ORDER BY id DESC",
            (agent_id,),
        ).fetchall()
        newest = conn.execute(
            "SELECT max(span_end) FROM understanding_nodes"
            " WHERE agent_id = %s AND depth = 1 AND engine_version LIKE 'chunk-%%'"
            " AND span_end >= %s AND span_end < %s",
            (agent_id, base, base + len(request)),
        ).fetchone()
        top_version = conn.execute(
            "SELECT max(compact_version) FROM understanding_chunk_jobs WHERE agent_id = %s",
            (agent_id,),
        ).fetchone()
    covered_to = 0 if newest is None or newest[0] is None else int(newest[0]) + 1 - base
    for version, status, start_index, end_at, end_msg_id in jobs:
        end_index = int(end_at)
        if 0 < end_index <= len(request) and request[end_index - 1].id == end_msg_id:
            # A failed job left its stretch undescribed: the close takes it up again.
            return int(version), int(start_index if status == "failed" else end_index), covered_to
    next_version = 0 if top_version is None or top_version[0] is None else int(top_version[0]) + 1
    return next_version, None, covered_to


def close_segment(db: Database, pool: ConnectionPool, agent_id: int) -> CloseResult:
    """Enqueue the job that closes the agent's live segment, or say why not (see the module)."""
    active = _active_job(pool, agent_id)
    if active is not None:
        return CloseResult(
            "active_job", job_id=active[0], detail=f"job {active[0]} is {active[1]}; wait for it"
        )
    history = load_checkpoint_history_full(db, agent_id)
    if not history.segment_starts:
        return CloseResult("empty", detail="the agent has no stored history")
    live = len(history.segment_starts) - 1
    head = history.segment_heads[live]
    request = [
        *([head] if head is not None else []),
        *history.messages[history.segment_starts[live] :],
    ]
    base = history.segment_starts[live] - (1 if head is not None else 0)
    version, job_start, covered_to = _live_segment_state(pool, agent_id, request, base)
    start = max(
        segment_head_len(request) if job_start is None else job_start,
        covered_to,  # never re-describe what a node already covers (no overlapping level-1 nodes)
    )
    end = sendable_len(request)
    if end <= start:
        return CloseResult(
            "empty",
            compact_version=version,
            start_index=start,
            end_index=end,
            detail="nothing past the last cut",
        )
    end_msg_id = request[end - 1].id
    if end_msg_id is None:
        return CloseResult(
            "empty", compact_version=version, start_index=start, end_index=end,
            detail="the segment's last message has no id, so it cannot be located later",
        )  # fmt: skip
    with write_transaction(pool) as conn:
        row = conn.execute(_ENQUEUE_SQL, (agent_id, version, start, end, end_msg_id)).fetchone()
    if row is None:
        return CloseResult(
            "empty",
            compact_version=version,
            start_index=start,
            end_index=end,
            detail="this stretch was already described",
        )
    return CloseResult(
        "enqueued", job_id=int(row[0]), compact_version=version, start_index=start, end_index=end
    )


class UnderstandingCloseResponse(BaseModel):
    agent_id: int
    status: Literal["enqueued", "empty", "active_job"]
    job_id: int | None
    compact_version: int | None
    start_index: int | None
    end_index: int | None
    detail: str


def _close_blocking(request: Request, agent_id: int) -> CloseResult:
    """Sync 404 guard + planning + enqueue — via to_thread."""
    with request.app.state.db_pool.connection() as conn:
        if not agent_exists(conn, agent_id):
            raise HTTPException(status_code=404, detail=f"agent {agent_id} not found")
    return close_segment(request.app.state.db, request.app.state.db_pool, agent_id)


@router.post("/api/agents/{agent_id}/understanding/close")
async def post_understanding_close(agent_id: int, request: Request) -> UnderstandingCloseResponse:
    """Describe the part of the agent's live compaction segment that no chunk has covered yet.

    Enqueues one understanding job from the end of the last chunk job (or the first message
    past the segment's head) to the last request the agent sent, like the closing chunk of a
    compaction. Short-lived agents never reach a size cut, so this is how their tail gets a
    node; nothing calls it automatically.

    The job runs on the existing consumer of the agent's host: the request is the agent's own
    conversation read from the stored checkpoint plus one instruction. Unlike the producers'
    chunks, which are sent while the agent's own requests keep the provider cache warm, the
    prefix of a manual close is usually cold, so the whole prefix is billed at the full input
    price (at DeepSeek's standard rate about $0.06 for a 390K-token segment), plus the
    model's reasoning output (about $0.008 per call).

    The answer is a status; nothing is written unless it is `enqueued`. `empty`: nothing lies
    past the last cut (or that stretch was already described). `active_job`: a
    job of this agent is pending or running (its id is returned); the queue keeps an agent's
    jobs in order, so wait for it and ask again. `compact_version`, `start_index` and `end_index`
    name the stretch (request-list indices of the live segment, head at 0). 404 when the agent
    does not exist; 503 when the stored history cannot be read. The job is described only where
    `AVA_UNDERSTANDING_ENABLED` is on (the consumer idles otherwise, and the job waits).
    """
    try:
        result = await asyncio.to_thread(_close_blocking, request, agent_id)
    except CheckpointReadError as exc:
        raise HTTPException(status_code=503, detail=f"history unreadable: {exc}") from exc
    return UnderstandingCloseResponse(
        agent_id=agent_id,
        status=result.status,
        job_id=result.job_id,
        compact_version=result.compact_version,
        start_index=result.start_index,
        end_index=result.end_index,
        detail=result.detail,
    )
