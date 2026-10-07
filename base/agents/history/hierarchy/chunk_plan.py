"""Chunk planning over a stored history — the live trigger rule, replayed.

`plan_replay` walks one compaction segment's request list exactly as the llm node's hook
(`agent/hooks/understanding_chunks.py`) would have as the segment grew: a chunk fires when a turn's
provider-reported `input_tokens` have grown by the threshold past the previous cut, and the stretch
left after the last cut is the segment's closing remainder. `plan_history` does it for every segment
of a stitched history. The manual build (`build.py`) and the preview replay tool
(`scripts/verify/understanding_replay.py`) both cut chunks with it.

Indices are positions in a segment's request list (its own SystemMessage head at 0).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from langchain_core.messages import AIMessage, AnyMessage

from base.agents.history.checkpoint import FullHistory
from base.agents.history.hierarchy.chunks import (
    Chunk,
    plan_chunk,
    plan_closing_chunk,
    segment_head_len,
    sendable_len,
)


@dataclass(frozen=True)
class PlannedChunk:
    """One chunk to enqueue; `closing` marks the segment's remainder."""

    chunk: Chunk
    end_msg_id: str | None
    input_tokens: int
    closing: bool
    segment: int = 0
    boundary_checkpoint_id: str | None = None


def plan_replay(
    messages: Sequence[AnyMessage], *, threshold: int, close: bool = True
) -> list[PlannedChunk]:
    """The chunks the live trigger rule cuts from one segment's request list, then its closing remainder.

    `messages` is the segment as the llm node sees it (SystemMessage head at 0). The
    walk mirrors `due_chunk_update`: an AI message is a turn whose request is everything
    before it; the first turn with usage records the baseline past the head. `close` False
    leaves the stretch after the last cut undescribed (the newest segment's tail).
    """
    out: list[PlannedChunk] = []
    cut_index = 0
    cut_tokens = 0
    for i, msg in enumerate(messages):
        if not isinstance(msg, AIMessage) or not msg.usage_metadata:
            continue
        input_tokens = int(msg.usage_metadata["input_tokens"])
        if input_tokens == 0:
            continue
        if cut_tokens == 0:
            cut_index = max(cut_index, segment_head_len(list(messages[:i])))
            cut_tokens = input_tokens
            continue
        chunk = plan_chunk(
            cut_index=cut_index,
            cut_tokens=cut_tokens,
            input_tokens=input_tokens,
            request_len=i,
            threshold=threshold,
        )
        if chunk is None:
            continue
        out.append(
            PlannedChunk(chunk, messages[chunk.end_index - 1].id, input_tokens, closing=False)
        )
        cut_index, cut_tokens = chunk.end_index, input_tokens
    closing = plan_closing_chunk(
        cut_index=max(cut_index, segment_head_len(list(messages))),
        request_len=sendable_len(list(messages)),
    )
    if close and closing is not None:
        out.append(PlannedChunk(closing, messages[closing.end_index - 1].id, 0, closing=True))
    return out


def segment_requests(history: FullHistory) -> list[list[AnyMessage]]:
    """Each segment as the llm node's request list: its own head, then its body."""
    count = len(history.segment_starts)
    out: list[list[AnyMessage]] = []
    for k in range(count):
        end = history.segment_starts[k + 1] if k + 1 < count else len(history.messages)
        head = history.segment_heads[k]
        body = cast("list[AnyMessage]", history.messages[history.segment_starts[k] : end])
        out.append([cast("AnyMessage", head), *body] if head is not None else body)
    return out


def plan_history(
    history: FullHistory,
    boundaries: Sequence[str],
    *,
    threshold: int,
    close_live: bool = False,
) -> list[PlannedChunk]:
    """Every segment's chunks; a boundary-closed segment ends in its closing chunk.

    `boundaries` are the compaction boundary checkpoint ids, oldest first (segment k is closed by
    `boundaries[k]`). The newest segment has no boundary: its tail stays undescribed as it would
    live unless `close_live` (a single-segment history is always closed, as the replay tool wants).
    """
    segments = segment_requests(history)
    planned: list[PlannedChunk] = []
    for k, request in enumerate(segments):
        closed = k < len(boundaries)
        keep_closing = closed or close_live or len(segments) == 1
        for p in plan_replay(request, threshold=threshold, close=keep_closing):
            planned.append(
                PlannedChunk(
                    p.chunk,
                    p.end_msg_id,
                    p.input_tokens,
                    p.closing,
                    segment=k,
                    boundary_checkpoint_id=boundaries[k] if closed and p.closing else None,
                )
            )
    return planned
