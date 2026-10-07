"""Chunk-triggered understanding — the trigger rule, the queue and chunk location.

The understanding layer describes an agent's context in chunks cut by size, not
by time or compaction alone. Within one compaction segment the llm node keeps a
cut (message index + the provider-reported `input_tokens` at that point,
`CompactState.understanding_cut_*`: where the next chunk begins by size, and the
token-growth baseline); once a request's `input_tokens` exceed the
cut's by the configured threshold, the stretch `[cut index, request length)` is
enqueued as a chunk job and the cut moves to the request's end. A compaction
enqueues the segment's closing remainder, so every segment is covered end to
end. Indices are positions in the llm node's request list — the segment's
messages with the standing SystemMessage head at 0.

A chunk call splits its stretch into groups, every unit in one (`leaf_groups.py`), so a chunk
is described on its own: jobs of one agent do not depend on each other.

This module holds the pure pieces and the queue SQL (`understanding_chunk_jobs`):

- `plan_chunk` / `plan_closing_chunk` — the trigger rule and the chunk cut;
- `enqueue_chunk` — best-effort enqueue (never raises, never blocks the turn);
- `claim_job` / `finish_job` / `release_job` / `backlog` — the consumer's queue;
- `write_group_nodes` — the groups' nodes written in one transaction;
- `locate_chunk` — a job's chunk inside the stitched checkpoint history, with
  the request prefix the model call rides and the stitched span the node stores.

Generation (the instruction, the model call) is `chunk_generate.py`; the
consumer loop lives with the agent host.

Segments are append-only, so a live chunk's end is verified by the id of its
last message at the recorded index: a checkpoint that has not caught up yet
(the checkpoint writer persists every Nth super-step) is `ChunkNotReadyError` and
retried; an id that sits elsewhere (a synthetic message inserted mid-segment by
crash repair shifts later indices) is `ChunkDriftError` and fails the job loudly.
A segment's closing chunk instead names its compaction boundary checkpoint and
is cut to what that snapshot holds.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, SystemMessage
from psycopg_pool import AsyncConnectionPool

from base import telemetry
from base.agents.history.checkpoint import FullHistory
from base.agents.history.hierarchy.store import SCHEMA_VERSION
from base.agents.messages.kwargs import AvaMsgType, read_ava_kwargs
from base.db.transaction import async_write_transaction
from base.log import logger

# Stored on the node rows a chunk produces, so a text traces to its rules.
CHUNK_ENGINE_VERSION = "chunk-0.2"
CHUNK_PROMPT_VERSION = "chunk-0.13"

# A retry of a not-yet-checkpointed chunk waits this long after the last claim,
# and a `running` claim older than the lease is taken over (a crashed host).
RETRY_SPACING_SECONDS = 30.0
CLAIM_LEASE_SECONDS = 900.0
MAX_ATTEMPTS = 20


@dataclass(frozen=True)
class Chunk:
    """A stretch of the llm node's request list: `[start_index, end_index)`."""

    start_index: int
    end_index: int


_FRAMEWORK_TYPES = frozenset(
    {
        AvaMsgType.SYSTEM_NOTE.value,
        AvaMsgType.COMPACT_SUMMARY.value,
        AvaMsgType.COMPACT_REQUEST.value,
    }
)


def _is_framework_injected(msg: BaseMessage) -> bool:
    return (
        isinstance(msg, SystemMessage)
        or read_ava_kwargs(msg).get("ava_msg_type") in _FRAMEWORK_TYPES
    )


def segment_head_len(messages: Sequence[BaseMessage]) -> int:
    """How many leading messages are the segment's head, never chunk material.

    The head is the run of framework-injected messages a segment opens with:
    the SystemMessage, the one-time system notes (timeout, timezone, agent id,
    memory index, ...), and in a compacted segment the carried-over summary
    and the notes after it. It ends at the first message that is not
    framework-injected — a new agent's first inbound, or the first thing the
    compacted agent did — which is where the segment's own material begins.
    """
    n = 0
    while n < len(messages) and _is_framework_injected(messages[n]):
        n += 1
    return n


def sendable_len(messages: Sequence[BaseMessage]) -> int:
    """The longest prefix that is a valid request: a trailing tool call whose
    result has not arrived is dropped (the agent compacting itself from code
    leaves its own call open). That prefix is the last request the agent sent."""
    if messages and isinstance(messages[-1], AIMessage) and messages[-1].tool_calls:
        return len(messages) - 1
    return len(messages)


def plan_chunk(
    *,
    cut_index: int,
    cut_tokens: int,
    input_tokens: int,
    request_len: int,
    threshold: int,
) -> Chunk | None:
    """The chunk to enqueue after a request of `request_len` messages, or None.

    `input_tokens` is what the provider reported for that request; the chunk
    fires when it has grown by `threshold` past the previous cut's.
    """
    if input_tokens - cut_tokens < threshold or request_len <= cut_index:
        return None
    return Chunk(cut_index, request_len)


def plan_closing_chunk(*, cut_index: int, request_len: int) -> Chunk | None:
    """The segment's closing remainder at compaction: from the last cut to the end."""
    if request_len <= cut_index:
        return None
    return Chunk(cut_index, request_len)


_ENQUEUE_SQL = (
    "INSERT INTO understanding_chunk_jobs"
    " (agent_id, compact_version, start_index, end_index, end_msg_id, boundary_checkpoint_id)"
    " VALUES (%s, %s, %s, %s, %s, %s)"
    " ON CONFLICT (agent_id, compact_version, start_index, end_index) DO NOTHING"
)


def _enqueue_failed(
    agent_id: int, compact_version: int, exc: Exception, chunk: Chunk | None = None
) -> None:
    """Report a failed enqueue: a warning plus the event (itself best-effort)."""
    logger.warning(
        "understanding enqueue failed for agent {agent} (version {version}, {chunk}): {error!r}",
        agent=agent_id,
        version=compact_version,
        chunk=chunk,
        error=exc,
    )
    try:
        telemetry.emit(
            "telemetry",
            "understanding_enqueue_failed",
            attributes={
                "agent_id": agent_id,
                "compact_version": compact_version,
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
    except Exception:
        logger.opt(exception=True).warning("understanding enqueue-failure event not emitted")


async def enqueue_chunk(
    pool: AsyncConnectionPool,
    agent_id: int,
    *,
    compact_version: int,
    chunk: Chunk,
    end_msg_id: str | None,
    boundary_checkpoint_id: str | None = None,
) -> bool:
    """Best-effort enqueue of one chunk; whether the job is (now) on the queue.

    Never raises: a failure is a warning plus the `understanding_enqueue_failed`
    event, and the caller keeps its cut where it was so the next trigger covers
    the same stretch again. A re-enqueue of the same chunk is a no-op success.
    A chunk whose last message carries no id is refused the same way: it could
    not be located in the checkpoint later.
    """
    if end_msg_id is None:
        _enqueue_failed(agent_id, compact_version, ValueError("the chunk's last message has no id"))
        return False
    try:
        async with async_write_transaction(pool) as conn, conn.cursor() as cur:
            await cur.execute(
                _ENQUEUE_SQL,
                (
                    agent_id,
                    compact_version,
                    chunk.start_index,
                    chunk.end_index,
                    end_msg_id,
                    boundary_checkpoint_id,
                ),
            )
    except Exception as exc:
        _enqueue_failed(agent_id, compact_version, exc, chunk)
        return False
    return True


@dataclass(frozen=True)
class ChunkJob:
    """One claimed queue row."""

    id: int
    agent_id: int
    compact_version: int
    start_index: int
    end_index: int
    end_msg_id: str
    boundary_checkpoint_id: str | None
    attempts: int


# Claim: a pending row not retried within the spacing, or a running row whose
# lease lapsed (its claimer died), and only the oldest live job of its agent (the
# next chunk starts where the previous one left an open group, so one agent's jobs
# never run side by side or out of order). SKIP LOCKED lets several runners poll
# one queue without ever taking the same row, and one runner's concurrent claims
# likewise. A replay (`segment_parallel`) narrows "oldest of its agent" to "oldest of
# its compaction segment": a closing chunk seals every open group, so segments carry
# nothing across; `agent_id` confines the claim to one agent.
_CLAIM_SQL = """
UPDATE understanding_chunk_jobs SET status = 'running', attempts = attempts + 1, claimed_at = now()
WHERE id = (
    SELECT j.id FROM understanding_chunk_jobs j
    WHERE ((j.status = 'pending'
            AND (j.claimed_at IS NULL OR j.claimed_at < now() - make_interval(secs => %(spacing)s)))
        OR (j.status = 'running' AND j.claimed_at < now() - make_interval(secs => %(lease)s)))
      AND NOT EXISTS (
        SELECT 1 FROM understanding_chunk_jobs o
        WHERE o.agent_id = j.agent_id AND o.id < j.id AND o.status IN ('pending', 'running')
          AND (NOT %(per_segment)s OR o.compact_version = j.compact_version))
      AND (%(agent)s::bigint IS NULL OR j.agent_id = %(agent)s::bigint)
    ORDER BY j.id
    FOR UPDATE OF j SKIP LOCKED
    LIMIT 1
)
RETURNING id, agent_id, compact_version, start_index, end_index, end_msg_id,
          boundary_checkpoint_id, attempts
"""


async def claim_job(
    pool: AsyncConnectionPool, *, agent_id: int | None = None, segment_parallel: bool = False
) -> ChunkJob | None:
    """Claim the oldest claimable job, or None when the queue has nothing due.

    Live consumers use the defaults: any agent, one agent's jobs strictly in order.
    `agent_id` and `segment_parallel` are the replay tool's (see `_CLAIM_SQL`).
    """
    async with async_write_transaction(pool) as conn, conn.cursor() as cur:
        await cur.execute(
            _CLAIM_SQL,
            {
                "spacing": RETRY_SPACING_SECONDS,
                "lease": CLAIM_LEASE_SECONDS,
                "per_segment": segment_parallel,
                "agent": agent_id,
            },
        )
        row = await cur.fetchone()
    if row is None:
        return None
    return ChunkJob(
        id=int(row[0]),
        agent_id=int(row[1]),
        compact_version=int(row[2]),
        start_index=int(row[3]),
        end_index=int(row[4]),
        end_msg_id=str(row[5]),
        boundary_checkpoint_id=None if row[6] is None else str(row[6]),
        attempts=int(row[7]),
    )


async def finish_job(
    pool: AsyncConnectionPool, job_id: int, *, status: str, error: str | None = None
) -> None:
    """Close a claimed job as `done`, `failed` or `skipped`."""
    if status not in ("done", "failed", "skipped"):
        raise ValueError(f"unknown terminal status {status!r}")
    async with async_write_transaction(pool) as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE understanding_chunk_jobs SET status = %s, error = %s, finished_at = now()"
            " WHERE id = %s",
            (status, error, job_id),
        )


async def release_job(pool: AsyncConnectionPool, job_id: int, *, error: str) -> None:
    """Hand a claimed job back to the queue; it is retried after the spacing."""
    async with async_write_transaction(pool) as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE understanding_chunk_jobs SET status = 'pending', error = %s WHERE id = %s",
            (error, job_id),
        )


@dataclass(frozen=True)
class Backlog:
    """The queue's depth: rows waiting, rows in flight, and the oldest wait."""

    pending: int
    running: int
    oldest_pending_age_seconds: float


async def backlog(pool: AsyncConnectionPool) -> Backlog:
    """One read of the queue's depth (the loop's per-round telemetry)."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FILTER (WHERE status = 'pending'),"
            " count(*) FILTER (WHERE status = 'running'),"
            " COALESCE(EXTRACT(EPOCH FROM now() - min(created_at)"
            "                  FILTER (WHERE status = 'pending')), 0)"
            " FROM understanding_chunk_jobs WHERE status IN ('pending', 'running')"
        )
        row = await cur.fetchone()
    assert row is not None, "an aggregate query always returns one row"  # noqa: S101
    return Backlog(int(row[0]), int(row[1]), float(row[2]))


class ChunkNotReadyError(Exception):
    """The checkpoint does not hold the chunk's end yet; retry later."""


class ChunkDriftError(Exception):
    """The chunk's end message is not where the job recorded it: indices drifted."""


class ChunkEmptyError(Exception):
    """Nothing is left of the chunk after the SystemMessage head is excluded."""


@dataclass(frozen=True)
class LocatedChunk:
    """A job's chunk inside the stitched history.

    `prefix` is the request the agent itself would have sent up to the chunk's
    end (the segment's head, then its messages); the chunk is
    `prefix[start_offset:]`. `span` is the stitched, inclusive message-index
    span the node row stores. `truncated` marks a closing chunk cut short by
    its boundary snapshot.
    """

    prefix: tuple[BaseMessage, ...]
    start_offset: int
    span: tuple[int, int]
    truncated: bool

    @property
    def messages(self) -> tuple[BaseMessage, ...]:
        """The chunk's own messages."""
        return self.prefix[self.start_offset :]


def locate_chunk(
    history: FullHistory,
    *,
    start_index: int,
    end_index: int,
    end_msg_id: str,
    closing_segment: int | None,
) -> LocatedChunk:
    """Find a job's chunk in the stitched history.

    A live chunk (`closing_segment` None) is searched newest segment first and
    verified by `end_msg_id` at its recorded position; a closing chunk is read
    from the named segment (the one its boundary checkpoint holds) and cut to
    what that snapshot kept.

    Raises:
        ChunkNotReadyError: the newest segment is shorter than the chunk's end.
        ChunkDriftError: no segment holds `end_msg_id` where the job recorded it.
        ChunkEmptyError: nothing of the chunk remains past the head.
    """
    starts = history.segment_starts
    count = len(starts)
    candidates = range(count - 1, -1, -1) if closing_segment is None else (closing_segment,)
    not_ready = False
    for k in candidates:
        head = history.segment_heads[k]
        offset = 1 if head is not None else 0
        seg_end = starts[k + 1] if k + 1 < count else len(history.messages)
        body = history.messages[starts[k] : seg_end]
        end_body = end_index - offset
        truncated = False
        if closing_segment is not None:
            if end_body > len(body):
                end_body, truncated = len(body), True
        elif len(body) < end_body:
            not_ready = not_ready or k == count - 1
            continue
        elif end_body < 1 or body[end_body - 1].id != end_msg_id:
            continue
        start_body = max(start_index - offset, 0)
        if end_body <= start_body:
            raise ChunkEmptyError(f"segment {k}: chunk [{start_index}, {end_index}) is empty")
        prefix = (*((head,) if head is not None else ()), *body[:end_body])
        return LocatedChunk(
            prefix=prefix,
            start_offset=offset + start_body,
            span=(starts[k] + start_body, starts[k] + end_body - 1),
            truncated=truncated,
        )
    if not_ready:
        raise ChunkNotReadyError(f"the newest segment is shorter than index {end_index}")
    raise ChunkDriftError(f"no segment holds message {end_msg_id!r} at index {end_index - 1}")


def message_time(messages: Sequence[BaseMessage], *, last: bool) -> datetime | None:
    """The first (or last) `ava_created_at` among `messages`, or None."""
    for msg in reversed(messages) if last else messages:
        stamp: Any = read_ava_kwargs(msg).get("ava_created_at")
        if isinstance(stamp, str) and stamp:
            return datetime.fromisoformat(stamp)
    return None


# A chunk's groups are the leaves of the tree: one depth-1 row per stitched span, upserted
# directly. `store.write_tree` is not used on purpose — its reconciliation
# deletes rows that overlap a reproduced span, and chunks are never a
# re-cut of a partition.
_UPSERT_NODE_SQL = """
INSERT INTO understanding_nodes (
    agent_id, depth, span_start, span_end, start_ts, end_ts, segment_key, text,
    text_hash, input_hash, children_count, model, engine_version, prompt_version,
    schema_version
) VALUES (%s, 1, %s, %s, %s, %s, %s, %s, %s, %s, 0, %s, %s, %s, %s)
ON CONFLICT (agent_id, depth, span_start, span_end) DO UPDATE SET
    start_ts = EXCLUDED.start_ts,
    end_ts = EXCLUDED.end_ts,
    segment_key = EXCLUDED.segment_key,
    text = EXCLUDED.text,
    text_hash = EXCLUDED.text_hash,
    input_hash = EXCLUDED.input_hash,
    model = EXCLUDED.model,
    engine_version = EXCLUDED.engine_version,
    prompt_version = EXCLUDED.prompt_version,
    schema_version = EXCLUDED.schema_version,
    updated_at = now()
"""


@dataclass(frozen=True)
class GroupNode:
    """One closed group, ready to store: its stitched inclusive span, times and summary."""

    span: tuple[int, int]
    start: datetime | None
    end: datetime | None
    text: str


async def write_group_nodes(
    pool: AsyncConnectionPool,
    job: ChunkJob,
    nodes: Sequence[GroupNode],
    *,
    model: str,
) -> None:
    """Upsert the depth-1 rows of a chunk's groups in one transaction.

    A row's times are the group's first and last `ava_created_at` (NULL when no message carries
    one, which keeps the row stored but unservable, like every untimed node).
    """
    async with async_write_transaction(pool) as conn, conn.cursor() as cur:
        for node in nodes:
            input_hash = hashlib.sha256(
                f"{CHUNK_ENGINE_VERSION}|{CHUNK_PROMPT_VERSION}|{job.agent_id}|{node.span}"
                f"|{job.end_msg_id}".encode()
            ).hexdigest()
            await cur.execute(
                _UPSERT_NODE_SQL,
                (
                    job.agent_id,
                    node.span[0],
                    node.span[1],
                    node.start,
                    node.end,
                    f"chunk:v{job.compact_version}",
                    node.text,
                    hashlib.sha256(node.text.encode()).hexdigest(),
                    input_hash,
                    model,
                    CHUNK_ENGINE_VERSION,
                    CHUNK_PROMPT_VERSION,
                    SCHEMA_VERSION,
                ),
            )


@dataclass(frozen=True)
class ChunkCall:
    """The raw record of one provider call made for a chunk.

    `response` is the provider's message as returned (None for a failed call,
    whose `error` is set); `round` counts the calls of one attempt from 0 (a
    refused tool call makes the next). `instruction` is the full trailing
    instruction sent; `prefix_len` / `start_offset` locate the chunk in the
    request. `kind` is `leaf` for the first request, or `group-correction` for a re-ask of the
    `<groups>` element in the same conversation (then `instruction` is the correction sent);
    `problem` is why the groups in this reply were refused (None = accepted).
    """

    round: int
    model: str
    instruction: str
    prefix_len: int
    start_offset: int
    response: BaseMessage | None
    duration_ms: float
    error: str | None
    kind: str = "leaf"
    problem: str | None = None


_INSERT_CALL_SQL = """
INSERT INTO understanding_chunk_calls (
    job_id, agent_id, attempt, round, model, instruction, prefix_len, start_offset,
    content, tool_calls, additional_kwargs, usage_metadata, response_metadata,
    duration_ms, error, kind, problem
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s, %s, %s)
"""


def _json(value: Any) -> str | None:
    """`value` as JSON text for a jsonb column; `default=str` keeps an odd leaf from losing the row."""
    return None if value is None else json.dumps(value, ensure_ascii=False, default=str)


async def write_chunk_calls(
    pool: AsyncConnectionPool, job: ChunkJob, calls: Sequence[ChunkCall]
) -> None:
    """Persist the raw record of a job attempt's provider calls, one row per call.

    Best-effort: a failure is a warning plus the `understanding_call_record_failed`
    event and never touches the job's outcome.
    """
    if not calls:
        return
    rows = [
        (
            job.id,
            job.agent_id,
            job.attempts,
            call.round,
            call.model,
            call.instruction,
            call.prefix_len,
            call.start_offset,
            _json(call.response.content if call.response else None),
            _json(getattr(call.response, "tool_calls", None)),
            _json(call.response.additional_kwargs if call.response else None),
            _json(getattr(call.response, "usage_metadata", None)),
            _json(call.response.response_metadata if call.response else None),
            call.duration_ms,
            call.error,
            call.kind,
            call.problem,
        )
        for call in calls
    ]
    try:
        async with async_write_transaction(pool) as conn, conn.cursor() as cur:
            await cur.executemany(_INSERT_CALL_SQL, rows)
    except Exception as exc:
        logger.opt(exception=True).warning(
            "understanding call record failed for job {job}", job=job.id
        )
        try:
            telemetry.emit(
                "telemetry",
                "understanding_call_record_failed",
                attributes={
                    "agent_id": job.agent_id,
                    "job_id": job.id,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
        except Exception:
            logger.opt(exception=True).warning("understanding call-record event not emitted")
