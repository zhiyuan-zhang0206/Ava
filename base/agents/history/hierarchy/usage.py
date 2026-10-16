# ava:ref base/agents/history/docs/range-read-contracts.ava.okf.md#history-range-inputs
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import accumulate

from langchain_core.messages import AIMessage, BaseMessage

from base.agents.messages.kwargs import read_ava_kwargs
from base.lm.pricing.cache_writes import cache_write_tokens


@dataclass(frozen=True)
class Usage:
    calls: int
    input: int
    cache_read: int
    output: int
    cache_write: int
    cost_usd: float
    cost_calls: int


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
    def __init__(self, messages: Sequence[BaseMessage], *, preceding: Usage = _ZERO) -> None:
        self._sums = list(accumulate((_call_usage(m) for m in messages), _add, initial=preceding))

    def prefix(self, stop: int) -> Usage:
        if not 0 <= stop < len(self._sums):
            raise IndexError(f"prefix {stop} is outside the {len(self._sums) - 1} messages")
        return self._sums[stop]

    def span(self, i0: int, i1: int) -> Usage:
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
    calls: int
    input: int
    cache_read: int
    output: int
    seconds: float
