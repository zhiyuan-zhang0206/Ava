"""Deterministic cost of a stretch of history — summed from what the messages carry.

Two figures hang on an understanding node, both computed by code (the model
only writes the node's text):

- the agent's own cost over the node's message span: the `usage_metadata` of
  the AIMessages inside it, summed (`MessageUsage.span`). `input` is the
  provider's total input tokens (cache reads included), the figure the
  telemetry's `in_total` reports;
- the cost of generating the node itself, from the raw record of the calls that wrote it
  (`understanding_chunk_calls` / `understanding_group_calls`), joined by the id the node row
  records (`job_id` / `check_key`, see `store.load_generation_costs`). A job's cost is shared by
  all the nodes of that job (a chunk call writes several groups), so it is the cost of the call
  that produced the node, not a part of it.

The agent's own cost sums level by level — a parent's span is the union of its
children's — so each node is one prefix-sum read.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import accumulate

from langchain_core.messages import AIMessage, BaseMessage


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
class GenerationUsage:
    """What generating one node cost: its calls' usage and wall time."""

    calls: int
    input: int
    cache_read: int
    output: int
    seconds: float
