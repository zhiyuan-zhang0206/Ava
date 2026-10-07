"""Understanding endpoints — /api/agents/{id}/understanding/*.

The understanding tree is built by the agent host's consumer from chunk jobs the producers enqueue
as an agent runs (`base/agents/history/hierarchy/chunks.py`). The one operator action is closing
an agent's live segment by hand: the part no chunk has described yet.

The producers of `chunks.py` cover a segment as it grows (size cuts) and when it is compacted
(the closing chunk). An agent that never reaches the first cut, or ends before it compacts,
leaves a tail nobody describes; this module enqueues that tail on demand
(`POST /api/agents/{id}/understanding/close`, below).

`close_segment` plans one job from the stored state alone, the way the producers would have:

- the segment is the newest of the checkpoint history; its version is its index (the producers'
  `compact_version`);
- the chunk starts where the last job of that segment left off — that job's end, else the
  segment's first message past its head (a failed job's stretch is taken up again, a skipped
  one's is not) — and ends at the last request the agent sent (`sendable_len`);
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
    status = 'pending', attempts = 0, error = NULL, claimed_at = NULL, finished_at = NULL,
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


def _start_index(pool: ConnectionPool, agent_id: int, version: int, head_len: int) -> int:
    """Where the segment's undescribed part begins."""
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT status, start_index, end_index FROM understanding_chunk_jobs"
            " WHERE agent_id = %s AND compact_version = %s ORDER BY id DESC LIMIT 1",
            (agent_id, version),
        ).fetchone()
    if row is None:
        return head_len
    status, start_index, end_index = row
    # A failed job left its stretch undescribed: the close takes it up again.
    return int(start_index if status == "failed" else end_index)


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
    version = len(history.segment_starts) - 1
    head = history.segment_heads[version]
    request = [
        *([head] if head is not None else []),
        *history.messages[history.segment_starts[version] :],
    ]
    start = _start_index(pool, agent_id, version, segment_head_len(request))
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
