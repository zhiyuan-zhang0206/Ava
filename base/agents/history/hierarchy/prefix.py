"""The request prefix of a generation call — the agent's own request head.

A node's generation request rides the agent's own leading messages so the
provider serves them from its prompt cache. Those bytes are only the agent's
when they are what the agent really sent: after a compaction the agent's head
is `[SystemMessage, summary, ...]`, never the whole stitched history, so a node
inside segment k takes segment k's own SystemMessage plus segment k's
messages up to the node's first message (`FullHistory` carries the segment
heads and body starts). The prefix therefore stays within the agent's own
context size — a compaction segment is bounded by the force-compact threshold —
instead of growing with every segment the agent has ever had.

The window cap is the guard behind that bound: when a prefix plus the node's
material would not fit the model's window (tokenizer drift included) the node
is sent material-only, losing the cache hit but never failing the call.
"""

from __future__ import annotations

import json
from bisect import bisect_right
from collections.abc import Sequence
from itertools import accumulate

from langchain_core.messages import AIMessage, BaseMessage

from base.agents.history.checkpoint import FullHistory, single_segment_history
from base.agents.history.hierarchy.tokens import count_tokens

# The prompt + text-only clause the request appends after the material, and the
# room the answer needs, in o200k tokens; generous on purpose — the cap guards
# against overflow, it is not a tuned economy.
REQUEST_OVERHEAD_TOKENS = 4000


def message_tokens(msg: BaseMessage) -> int:
    """Estimated o200k tokens of one message (content plus tool-call arguments).

    A JSON serialization overstates syntax slightly — the safe direction for a
    cap — and one caliber is used everywhere in the engine (`tokens.py`).
    """
    parts = [json.dumps(msg.content, ensure_ascii=False, default=str)]
    if isinstance(msg, AIMessage) and msg.tool_calls:
        parts.append(json.dumps(msg.tool_calls, ensure_ascii=False, default=str))
    return count_tokens("".join(parts))


class PrefixPlanner:
    """Picks each node's request prefix from the segment layout of the history."""

    def __init__(
        self,
        history: FullHistory,
        *,
        window_tokens: int | None = None,
        window_fraction: float = 0.8,
    ) -> None:
        """`window_tokens` is the agent model's context window; None = no cap."""
        self._messages = history.messages
        self._heads = history.segment_heads
        self._starts = history.segment_starts
        self._cap = None if window_tokens is None else int(window_tokens * window_fraction)
        # Per-segment cumulative body tokens, tokenized lazily: only segments
        # holding a node that needs a model call are ever counted.
        self._cumulative: dict[int, list[int]] = {}
        self.capped = 0

    @classmethod
    def single_segment(
        cls,
        messages: Sequence[BaseMessage],
        *,
        window_tokens: int | None = None,
        window_fraction: float = 0.8,
    ) -> PrefixPlanner:
        """A planner over a history that is one snapshot (no compaction join)."""
        return cls(
            single_segment_history(list(messages)),
            window_tokens=window_tokens,
            window_fraction=window_fraction,
        )

    def _body_tokens_before(self, segment: int, cut: int) -> int:
        """Tokens of segment `segment`'s body messages preceding index `cut`."""
        cumulative = self._cumulative.get(segment)
        if cumulative is None:
            start = self._starts[segment]
            end = (
                self._starts[segment + 1]
                if segment + 1 < len(self._starts)
                else len(self._messages)
            )
            cumulative = [0, *accumulate(message_tokens(m) for m in self._messages[start:end])]
            self._cumulative[segment] = cumulative
        return cumulative[min(cut - self._starts[segment], len(cumulative) - 1)]

    def prefix_for(self, cut: int, material_tokens: int) -> tuple[BaseMessage, ...]:
        """The request prefix for a node whose first message sits at `cut`.

        Empty (the material-only request) when the node's segment has no head
        SystemMessage or the prefix plus material would overflow the cap.
        """
        segment = bisect_right(self._starts, cut) - 1
        if segment < 0:
            return ()
        head = self._heads[segment]
        if head is None:
            return ()
        if self._cap is not None:
            used = (
                message_tokens(head)
                + self._body_tokens_before(segment, cut)
                + material_tokens
                + REQUEST_OVERHEAD_TOKENS
            )
            if used > self._cap:
                self.capped += 1
                return ()
        return (head, *self._messages[self._starts[segment] : cut])
