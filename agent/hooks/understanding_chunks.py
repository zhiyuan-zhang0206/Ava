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
  `settings.agent.understanding_chunk_tokens`, enqueue
  `[cut index, request length)` and return the `compact` update that moves
  the cut. A failed enqueue leaves the cut where it was, so the next turn
  covers the same stretch again.
- `enqueue_closing_chunk` — at compaction: the segment's remainder from the
  last cut to its end, tied to the boundary checkpoint that holds the segment.

Both are best-effort and silent when `AVA_UNDERSTANDING_ENABLED` is off; neither
may fail the agent's turn.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, AnyMessage
from psycopg_pool import AsyncConnectionPool

from agent.state_channels import CompactState
from base.agents.history.hierarchy.chunks import (
    enqueue_chunk,
    plan_chunk,
    plan_closing_chunk,
    segment_head_len,
    sendable_len,
)
from base.config import settings


async def due_chunk_update(
    compact: CompactState,
    request: list[AnyMessage],
    final_msg: AIMessage,
    *,
    pool: AsyncConnectionPool | None,
    agent_id: int,
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
        threshold=settings.agent.understanding_chunk_tokens,
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
