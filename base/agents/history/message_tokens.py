"""Per-message token counts recovered from the provider's own request usage.

Every LLM request is one AIMessage, and the next request carries the previous
turn's output (thinking included) plus whatever entered the context since. So
for adjacent requests `j`, `j+1` of one segment:

    input_{j+1} - input_j = output_j + sum(context of the non-AI messages between)

`output_j` is the AIMessage's `usage_metadata.output_tokens`; what remains is the
true provider-tokenizer weight of the messages in between. One message in the
interval takes it whole (`exact`); several share it in proportion to the
fitted token estimate of `base/agents/messages/text_chars.py` (`split`). Everything the usage cannot pin
down falls back to that estimate (`estimated`):

- the interval's difference is negative (crash repair rewrote history, a
  compaction-like edit) or the two requests ran on different models (different
  tokenizers);
- an AIMessage on either side of the interval lacks usage;
- the tail after a segment's last AIMessage (no later request exists).

A segment's head (its SystemMessage plus the messages before the first request)
is anchored by the first request's `input_tokens`, split by the same estimate;
the head also absorbs whatever fixed overhead a request carries (tool schemas).
An AIMessage's own record is `exact` whenever it has `output_tokens`: its
generation and its context footprint are that number.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, cast

from langchain_core.messages import AIMessage, BaseMessage, SystemMessage

from base.agents.history.checkpoint import FullHistory
from base.agents.messages.text_chars import (
    ai_message_texts,
    estimate_message_tokens,
    estimate_text_tokens,
)

TokenSource = Literal["exact", "split", "estimated"]
_SOURCES: tuple[TokenSource, ...] = ("exact", "split", "estimated")
_WEIGHT_SCALE = 1000  # estimate -> integer weight for the exact apportionment


@dataclass(frozen=True)
class MessageTokens:
    """One message's token weight. `generation_tokens` is set only for an
    AIMessage (what the model generated); `context_tokens` is what the message
    occupies in later requests."""

    context_tokens: int
    generation_tokens: int | None
    source: TokenSource


@dataclass(frozen=True)
class SegmentTokens:
    """Per-message tokens of one compaction segment. `head` is the segment's
    SystemMessage (None when the snapshot had none); `messages` aligns with the
    segment body. `last_input_tokens` is the `input_tokens` of the segment's
    last request that reported usage."""

    head: MessageTokens | None
    messages: tuple[MessageTokens, ...]
    last_input_tokens: int | None


@dataclass(frozen=True)
class TokenTotal:
    """An aggregate of token values (a segment, a Context Breakdown bucket).

    `estimated` is True when ANY part is `split` or `estimated` (the frontend
    appends "(estimated)" to the value); `exact_fraction` is the share of
    `tokens` that came from `exact` parts (1.0 for an empty aggregate)."""

    tokens: int
    estimated: bool
    exact_fraction: float


def total_of(records: Sequence[MessageTokens]) -> TokenTotal:
    """Aggregate `context_tokens` of `records` with the exactness rule above."""
    tokens = sum(r.context_tokens for r in records)
    exact = sum(r.context_tokens for r in records if r.source == "exact")
    return TokenTotal(
        tokens=tokens,
        estimated=any(r.source != "exact" for r in records),
        exact_fraction=exact / tokens if tokens else 1.0,
    )


@dataclass(frozen=True)
class PartTokens:
    """One part of a message (an AIMessage's reasoning / output / tool_call, a
    system-prompt section). A part's share is always an estimate of how the
    whole divides, so `source` is `split` -- or `estimated` when the whole is
    itself only estimated. A lone part inherits the whole's source."""

    tokens: int
    source: TokenSource


@dataclass(frozen=True)
class SegmentSummary:
    """A segment's totals (head included in `context_tokens`) and how much of
    them each `source` accounts for. `estimated` / `exact_fraction` follow
    `TokenTotal`."""

    message_count: int
    head_tokens: int
    context_tokens: int
    generation_tokens: int
    tokens_by_source: dict[TokenSource, int]
    messages_by_source: dict[TokenSource, int]
    last_input_tokens: int | None
    estimated: bool
    exact_fraction: float


def _estimate(msg: BaseMessage) -> int:
    return round(estimate_message_tokens(msg))


def _usage(msg: BaseMessage, key: str) -> int | None:
    """A positive `usage_metadata` count of an AIMessage, else None."""
    if not isinstance(msg, AIMessage) or msg.usage_metadata is None:
        return None
    value = cast(dict[str, object], msg.usage_metadata).get(key)
    return value if isinstance(value, int) and value > 0 else None


def _model_name(msg: AIMessage) -> str | None:
    meta = msg.response_metadata
    name = meta.get("model_name") or meta.get("model")
    return name if isinstance(name, str) and name else None


def apportion(weights: Sequence[int], total: int) -> list[int]:
    """Split `total` across `weights` proportionally, summing exactly (largest
    remainder); all-zero weights split evenly."""
    if not weights:
        return []
    basis = list(weights) if sum(weights) > 0 else [1] * len(weights)
    denom = sum(basis)
    shares = [total * w // denom for w in basis]
    by_remainder = sorted(range(len(basis)), key=lambda i: -(total * basis[i] % denom))
    for i in by_remainder[: total - sum(shares)]:
        shares[i] += 1
    return shares


def split_parts(
    texts: Mapping[str, str], total: int, total_source: TokenSource
) -> dict[str, PartTokens]:
    """Divide `total` tokens among named text parts by the fitted estimate,
    summing exactly. Parts of one message are never measured individually, so
    the result is `split` (or `estimated` when `total` is); one part keeps
    `total_source`."""
    if len(texts) == 1:
        return {name: PartTokens(total, total_source) for name in texts}
    source: TokenSource = "estimated" if total_source == "estimated" else "split"
    weights = [round(estimate_text_tokens(t) * _WEIGHT_SCALE) for t in texts.values()]
    return {
        name: PartTokens(share, source)
        for name, share in zip(texts, apportion(weights, total), strict=True)
    }


def ai_message_parts(msg: AIMessage, record: MessageTokens) -> dict[str, PartTokens]:
    """An AIMessage's reasoning / output / tool_call split of its `record`."""
    return split_parts(ai_message_texts(msg), record.context_tokens, record.source)


def _allocate(msgs: Sequence[BaseMessage], total: int) -> list[MessageTokens]:
    """Give `total` real tokens to `msgs`: whole to a lone message, else by estimate."""
    if len(msgs) == 1:
        return [MessageTokens(total, None, "exact")]
    shares = apportion([round(estimate_message_tokens(m) * _WEIGHT_SCALE) for m in msgs], total)
    return [MessageTokens(share, None, "split") for share in shares]


def _estimated(msg: BaseMessage) -> MessageTokens:
    est = _estimate(msg)
    return MessageTokens(est, est if isinstance(msg, AIMessage) else None, "estimated")


def _ai_record(msg: AIMessage) -> MessageTokens:
    out = _usage(msg, "output_tokens")
    return MessageTokens(out, out, "exact") if out is not None else _estimated(msg)


def _interval_total(prev: AIMessage, nxt: AIMessage) -> int | None:
    """Real tokens of the non-AI messages between two adjacent requests, or None
    when the usage cannot be trusted (missing, model switch, negative difference)."""
    prev_in, nxt_in, prev_out = (
        _usage(prev, "input_tokens"),
        _usage(nxt, "input_tokens"),
        _usage(prev, "output_tokens"),
    )
    if prev_in is None or nxt_in is None or prev_out is None:
        return None
    prev_model, nxt_model = _model_name(prev), _model_name(nxt)
    if prev_model is not None and nxt_model is not None and prev_model != nxt_model:
        return None
    remainder = nxt_in - prev_in - prev_out
    return remainder if remainder >= 0 else None


def _head_records(
    head: SystemMessage | None, pre: Sequence[BaseMessage], first_input: int | None
) -> list[MessageTokens]:
    """Records for `[head] + pre`, anchored by the first request's `input_tokens`."""
    group: list[BaseMessage] = ([head] if head is not None else []) + list(pre)
    if first_input is not None and group:
        return _allocate(group, first_input)
    return [_estimated(m) for m in group]


def _interval_records(body: Sequence[BaseMessage], here: int, there: int) -> list[MessageTokens]:
    """Records for the non-AI messages between request `here` and the next request
    `there` (`there == len(body)` when none follows: the tail, estimated)."""
    between = body[here + 1 : there]
    total = (
        _interval_total(cast(AIMessage, body[here]), cast(AIMessage, body[there]))
        if between and there < len(body)
        else None
    )
    if total is None:
        return [_estimated(m) for m in between]
    return _allocate(between, total)


def segment_tokens(head: SystemMessage | None, body: Sequence[BaseMessage]) -> SegmentTokens:
    """Per-message tokens of one segment: its head and its body messages."""
    ai_at = [i for i, m in enumerate(body) if isinstance(m, AIMessage)]
    out: list[MessageTokens | None] = [None] * len(body)
    for i in ai_at:
        out[i] = _ai_record(cast(AIMessage, body[i]))

    first = ai_at[0] if ai_at else len(body)
    first_input = _usage(body[first], "input_tokens") if ai_at else None
    head_records = _head_records(head, body[:first], first_input)
    head_tokens = head_records.pop(0) if head is not None else None
    out[:first] = head_records

    for here, there in zip(ai_at, [*ai_at[1:], len(body)][: len(ai_at)], strict=True):
        out[here + 1 : there] = _interval_records(body, here, there)

    last_input = next(
        (u for i in reversed(ai_at) if (u := _usage(body[i], "input_tokens")) is not None), None
    )
    return SegmentTokens(head_tokens, tuple(cast(list[MessageTokens], out)), last_input)


def history_segment_tokens(history: FullHistory) -> tuple[SegmentTokens, ...]:
    """`segment_tokens` for every segment of a stitched history, in order."""
    starts = history.segment_starts
    ends = [*starts[1:], len(history.messages)][: len(starts)]
    return tuple(
        segment_tokens(head, history.messages[start:end])
        for head, start, end in zip(history.segment_heads, starts, ends, strict=True)
    )


def history_message_tokens(history: FullHistory) -> list[MessageTokens]:
    """One record per `history.messages` entry (segment heads excluded; read them
    from `history_segment_tokens`)."""
    return [m for seg in history_segment_tokens(history) for m in seg.messages]


def summarize_segment(segment: SegmentTokens) -> SegmentSummary:
    """Totals of one segment for list / gauge views."""
    records = ([segment.head] if segment.head is not None else []) + list(segment.messages)
    tokens: dict[TokenSource, int] = dict.fromkeys(_SOURCES, 0)
    counts: dict[TokenSource, int] = dict.fromkeys(_SOURCES, 0)
    for r in records:
        tokens[r.source] += r.context_tokens
        counts[r.source] += 1
    total = total_of(records)
    return SegmentSummary(
        message_count=len(segment.messages),
        head_tokens=segment.head.context_tokens if segment.head is not None else 0,
        context_tokens=sum(tokens.values()),
        generation_tokens=sum(r.generation_tokens or 0 for r in records),
        tokens_by_source=tokens,
        messages_by_source=counts,
        last_input_tokens=segment.last_input_tokens,
        estimated=total.estimated,
        exact_fraction=total.exact_fraction,
    )


def summarize_segments(history: FullHistory) -> tuple[SegmentSummary, ...]:
    """Per-segment totals of a stitched history."""
    return tuple(summarize_segment(s) for s in history_segment_tokens(history))
