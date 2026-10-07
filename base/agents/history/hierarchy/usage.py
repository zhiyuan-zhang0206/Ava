"""Deterministic cost of a stretch of history — summed from what the messages carry.

Two figures hang on an understanding node, both computed by code (the model
only writes the node's text):

- the agent's own cost over the node's message span: the `usage_metadata` of
  the AIMessages inside it, summed (`MessageUsage.span`). `input` is the
  provider's total input tokens (cache reads included), the figure the
  telemetry's `in_total` reports;
- the cost of generating the node itself, from the raw record of its
  understanding calls (`understanding_chunk_calls`): `generation_by_span` finds
  each call's node from where the call's request put the chunk, so no node row
  needs to remember its job.

The agent's own cost sums level by level — a parent's span is the union of its
children's — so each node is one prefix-sum read.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import accumulate
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage

from base.agents.history.checkpoint import FullHistory


@dataclass(frozen=True)
class Usage:
    """Token usage over some set of provider calls."""

    calls: int
    input: int
    cache_read: int
    output: int


def _call_usage(msg: BaseMessage) -> Usage:
    if not isinstance(msg, AIMessage) or not msg.usage_metadata:
        return Usage(0, 0, 0, 0)
    meta = msg.usage_metadata
    details = meta.get("input_token_details") or {}
    return Usage(
        calls=1,
        input=int(meta.get("input_tokens", 0)),
        cache_read=int(details.get("cache_read", 0)),
        output=int(meta.get("output_tokens", 0)),
    )


def _add(a: Usage, b: Usage) -> Usage:
    return Usage(
        a.calls + b.calls, a.input + b.input, a.cache_read + b.cache_read, a.output + b.output
    )


class MessageUsage:
    """Prefix sums of the AIMessage usage of one message list; `span` is a constant-time read."""

    def __init__(self, messages: Sequence[BaseMessage]) -> None:
        self._sums = [Usage(0, 0, 0, 0), *accumulate((_call_usage(m) for m in messages), _add)]

    def span(self, i0: int, i1: int) -> Usage:
        """The usage of messages `i0..i1` inclusive."""
        if not 0 <= i0 <= i1 < len(self._sums) - 1:
            raise IndexError(f"span [{i0}, {i1}] is outside the {len(self._sums) - 1} messages")
        low, high = self._sums[i0], self._sums[i1 + 1]
        return Usage(
            high.calls - low.calls,
            high.input - low.input,
            high.cache_read - low.cache_read,
            high.output - low.output,
        )


@dataclass(frozen=True)
class CallRecord:
    """The slice of one `understanding_chunk_calls` row the cost needs.

    `compact_version` is the job's (the segment index its chunk lived in);
    `start_offset` / `prefix_len` locate the chunk in the request the call sent.
    """

    compact_version: int
    start_offset: int
    prefix_len: int
    usage_metadata: Mapping[str, Any] | None
    duration_ms: float


@dataclass(frozen=True)
class GenerationUsage:
    """What generating one node cost: its calls' usage and wall time."""

    calls: int
    input: int
    cache_read: int
    output: int
    seconds: float


def generation_by_span(
    history: FullHistory, records: Sequence[CallRecord]
) -> dict[tuple[int, int], GenerationUsage]:
    """Each call's cost summed per node, keyed by the node's stitched `(span_start, span_end)`.

    A call's request is the segment's head then its messages up to the chunk's
    end; the chunk begins at `start_offset`. Back in the stitched history that
    is `segment_starts[k] + start_offset - head` through
    `segment_starts[k] + prefix_len - head - 1`, `k` the job's segment and
    `head` 1 when the segment kept its SystemMessage. A call whose segment is
    not in the history yields no entry.
    """
    totals: dict[tuple[int, int], list[float]] = {}
    for rec in records:
        k = rec.compact_version
        if not 0 <= k < len(history.segment_starts):
            continue
        head = 1 if history.segment_heads[k] is not None else 0
        base = history.segment_starts[k] - head
        span = (base + rec.start_offset, base + rec.prefix_len - 1)
        meta: Mapping[str, Any] = rec.usage_metadata or {}
        details: Mapping[str, Any] = meta.get("input_token_details") or {}
        row = totals.setdefault(span, [0, 0, 0, 0, 0.0])
        row[0] += 1
        row[1] += int(meta.get("input_tokens", 0))
        row[2] += int(details.get("cache_read", 0))
        row[3] += int(meta.get("output_tokens", 0))
        row[4] += rec.duration_ms / 1000
    return {
        span: GenerationUsage(int(c), int(i), int(r), int(o), s)
        for span, (c, i, r, o, s) in totals.items()
    }
