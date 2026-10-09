"""Agent-side half of chunk-triggered understanding: when to enqueue a chunk.

Two producers feed `understanding_chunk_jobs` (the rule and the queue are
`base/agents/history/hierarchy/chunks.py`; the consumer is the agent host's
loop):

- `due_chunk_update` — after every llm turn. A segment's first turn only
  records the baseline: the cut moves past the head (system prompt and the
  carried-over compact summary, both already understood) and its tokens
  become the reference, so the fixed head never counts as material. After
  that, once the provider-reported `input_tokens` exceed those at the
  segment's previous cut by
  `chunk_threshold` (`AVA_UNDERSTANDING_CHUNK_RATIO` x the agent model's soft compaction threshold), enqueue
  `[cut index, request length)` and return the `compact` update that moves
  the cut. A failed enqueue leaves the cut where it was, so the next turn
  covers the same stretch again.
- `enqueue_closing_chunk` — at compaction: the segment's remainder from the
  last cut to its end, tied to the boundary checkpoint that holds the segment.

Both are best-effort and silent when `AVA_UNDERSTANDING_ENABLED` is off; neither
may fail the agent's turn.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any

from langchain_core.messages import AIMessage, AnyMessage
from psycopg_pool import AsyncConnectionPool

from agent.state_channels import CompactState
from base import telemetry
from base.agents.history.hierarchy.chunks import (
    chunk_threshold,
    enqueue_chunk,
    plan_chunk,
    plan_closing_chunk,
    segment_head_len,
    sendable_len,
)
from base.agents.messages.kwargs import message_read_time
from base.config import settings
from base.host.env.agent_slices import ModelOverrides
from base.lm.catalog import ModelCatalog
from base.log import logger


async def due_chunk_update(
    compact: CompactState,
    request: list[AnyMessage],
    final_msg: AIMessage,
    *,
    pool: AsyncConnectionPool | None,
    agent_id: int,
    model: str,
    overrides: ModelOverrides,
    catalog: ModelCatalog,
) -> dict[str, Any]:
    """The state update after one llm turn: `{}`, or the moved cut once a chunk is enqueued.

    `request` is the message list the turn sent (head included); `final_msg`
    carries the provider's usage for it.
    """
    if pool is None or not settings.agent.understanding_enabled:
        return {}
    usage = final_msg.usage_metadata
    input_tokens = int(usage["input_tokens"]) if usage else 0
    if input_tokens == 0:
        return {}
    if compact.understanding_cut_tokens == 0:
        return {
            "compact": compact.model_copy(
                update={
                    "understanding_cut_index": max(
                        compact.understanding_cut_index, segment_head_len(request)
                    ),
                    "understanding_cut_tokens": input_tokens,
                }
            )
        }
    chunk = plan_chunk(
        cut_index=compact.understanding_cut_index,
        cut_tokens=compact.understanding_cut_tokens,
        input_tokens=input_tokens,
        request_len=len(request),
        threshold=chunk_threshold(
            model, overrides, settings.agent.understanding_chunk_ratio, catalog=catalog
        ),
    )
    if chunk is None:
        return {}
    enqueued = await enqueue_chunk(
        pool,
        agent_id,
        compact_version=compact.version,
        chunk=chunk,
        end_msg_id=request[chunk.end_index - 1].id,
    )
    if not enqueued:
        return {}
    return {
        "compact": compact.model_copy(
            update={
                "understanding_cut_index": chunk.end_index,
                "understanding_cut_tokens": input_tokens,
            }
        )
    }


async def enqueue_closing_chunk(
    compact: CompactState,
    messages: list[AnyMessage],
    *,
    pool: AsyncConnectionPool | None,
    agent_id: int,
    boundary: str | None,
) -> None:
    """Enqueue the closing segment's remainder at compaction (best-effort).

    `messages` is the segment as the compaction saw it; `boundary`
    is the checkpoint stamped for it (None = the stamp failed, nothing to anchor
    the read on, so nothing is enqueued).
    """
    if pool is None or boundary is None or not settings.agent.understanding_enabled:
        return
    chunk = plan_closing_chunk(
        cut_index=max(compact.understanding_cut_index, segment_head_len(messages)),
        request_len=sendable_len(messages),
    )
    if chunk is None:
        return
    await enqueue_chunk(
        pool,
        agent_id,
        compact_version=compact.version,
        chunk=chunk,
        end_msg_id=messages[chunk.end_index - 1].id,
        boundary_checkpoint_id=boundary,
    )


# How long a compaction waits for the checkpoint to hold the state's last message before it stamps
# the boundary anyway: the graph persists a super-step's checkpoint asynchronously, so the newest
# row can lag the state by one step.
SNAPSHOT_WAIT_SECONDS = 5.0
_SNAPSHOT_POLL_SECONDS = 0.5

_NEWEST_CHECKPOINT_TS = (
    "SELECT checkpoint->>'ts' FROM checkpoints WHERE thread_id = %s AND checkpoint_ns = ''"
    " ORDER BY checkpoint_id DESC LIMIT 1"
)


def _read_time(message: AnyMessage) -> datetime | None:
    """When the model read `message` (`ava_picked_up_at`, else `ava_created_at`) as an aware
    time; None for a missing, unparsable or timezone-less stamp (an old message)."""
    stamp = message_read_time(message)
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


async def _newest_checkpoint_ts(pool: AsyncConnectionPool, agent_id: int) -> datetime | None:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_NEWEST_CHECKPOINT_TS, (str(agent_id),))
        row = await cur.fetchone()
    return None if row is None or not row[0] else datetime.fromisoformat(str(row[0]))


async def await_snapshot(pool: AsyncConnectionPool | None, state: Any, agent_id: int) -> None:
    """Wait (bounded) until the newest checkpoint of the agent is at least as new as the `state`'s
    last message.

    Called by the compact paths right before the boundary is stamped: the stamped checkpoint is
    the full-snapshot record of the segment, and the segment's closing chunk is read from it, so a
    checkpoint one super-step behind would lose the segment's last turns for good (from the
    stitched history as well). A checkpoint is written after the messages of its step, so one whose
    time is not before the last message's read time (`message_read_time`: the pickup time of an
    injected message, else its creation) holds it. One cheap read per poll and a bounded sleep;
    nothing about the turn, the state or the request prefix changes. When the wait cannot succeed
    (it times out, or the last message has no usable time: none, or no timezone) the boundary is
    stamped anyway and `understanding_snapshot_lag` is emitted. Best-effort, silent only when
    understanding is off or there is no state.
    """
    messages: list[AnyMessage] = [] if state is None else list(state.messages)
    if pool is None or not messages or not settings.agent.understanding_enabled:
        return
    last = _read_time(messages[-1])
    started = time.monotonic()
    while last is not None:
        try:
            newest = await _newest_checkpoint_ts(pool, agent_id)
        except Exception:
            logger.opt(exception=True).warning("snapshot check failed for agent {}", agent_id)
            return
        if newest is not None and newest >= last:
            return
        if time.monotonic() - started >= SNAPSHOT_WAIT_SECONDS:
            break
        await asyncio.sleep(_SNAPSHOT_POLL_SECONDS)
    logger.warning(
        "compaction of agent {agent} stamps its boundary before the checkpoint is known to hold "
        "the last message {message}",
        agent=agent_id,
        message=messages[-1].id,
    )
    telemetry.emit(
        "telemetry",
        "understanding_snapshot_lag",
        attributes={"agent_id": agent_id, "waited_seconds": round(time.monotonic() - started, 1)},
    )
