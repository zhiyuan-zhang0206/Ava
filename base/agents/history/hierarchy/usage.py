"""Deterministic cost of a stretch of history — summed from what the messages carry.

Two figures hang on an understanding node, both computed by code (the model
only writes the node's text):

- the agent's own cost over the node's message span: the `usage_metadata` (and the recorded
  `ava_usage` cost) of the AIMessages inside it, summed (`MessageUsage.span`). `input` is the
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

from base.agents.messages.kwargs import read_ava_kwargs
from base.lm.pricing.cache_writes import cache_write_tokens


@dataclass(frozen=True)
class Usage:
    """Token usage over some set of provider calls."""

    calls: int
    input: int
    cache_read: int
    output: int
    cache_write: int
    cost_usd: float
    cost_calls: int
    """How many of `calls` carry a recorded cost; `cost_usd` sums those only. A call without one
    (predating the record, or unpriced) has an unknown cost, never an estimate."""


_ZERO = Usage(0, 0, 0, 0, 0, 0.0, 0)


def _call_usage(msg: BaseMessage) -> Usage:
    if not isinstance(msg, AIMessage) or not msg.usage_metadata:
        return _ZERO
    meta = msg.usage_metadata
    details = meta.get("input_token_details") or {}
    cost = read_ava_kwargs(msg).get("ava_usage", {}).get("cost_usd")
    return Usage(
        calls=1,
        input=int(meta.get("input_tokens", 0)),
        cache_read=int(details.get("cache_read", 0)),
        output=int(meta.get("output_tokens", 0)),
        cache_write=sum(cache_write_tokens(details)),
        cost_usd=0.0 if cost is None else float(cost),
        cost_calls=0 if cost is None else 1,
    )


def _add(a: Usage, b: Usage) -> Usage:
    return Usage(
        a.calls + b.calls,
        a.input + b.input,
        a.cache_read + b.cache_read,
        a.output + b.output,
        a.cache_write + b.cache_write,
        a.cost_usd + b.cost_usd,
        a.cost_calls + b.cost_calls,
    )


class MessageUsage:
    """Prefix sums of the AIMessage usage of one message list; `span` is a constant-time read."""

    def __init__(self, messages: Sequence[BaseMessage]) -> None:
        self._sums = [_ZERO, *accumulate((_call_usage(m) for m in messages), _add)]

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
            high.cache_write - low.cache_write,
            round(high.cost_usd - low.cost_usd, 9),
            high.cost_calls - low.cost_calls,
        )


@dataclass(frozen=True)
class GenerationUsage:
    """What generating one node cost: its calls' usage and wall time."""

    calls: int
    input: int
    cache_read: int
    output: int
    seconds: float
