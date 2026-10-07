"""Per-message token counts recovered from the provider's own request usage.

Every LLM request is one AIMessage and every request re-sends the previous
turn's output (thinking included) plus whatever entered the context since. For
two adjacent requests `a`, `b` of one segment:

    input_b - input_a = output_a + sum(context of the messages between)

`output_a` is the AIMessage's `usage_metadata.output_tokens`, so what remains is
the provider-tokenizer weight of the messages between. Every value is anchored
to a provider-reported total; there is no unanchored estimate. Two sources:

- `exact`: the provider's own number -- an AIMessage's `output_tokens`, or the
  whole remainder when one message sits in the interval.
- `estimated`: a provider-reported total shared among several messages in
  proportion to the fitted estimator (`base/agents/messages/token_estimate.py`),
  summing exactly to the total.

Boundaries, all re-anchoring on a later provider total instead of guessing:

- Segment head (SystemMessage + messages before the first request): the first
  request's `input_tokens`, shared among them.
- AIMessage without usage: not an anchor; the interval merges forward into the
  next request that has usage.
- Negative difference (crash repair, rewritten history): the offending anchor is
  dropped and the interval merges; if no earlier anchor is consistent the later
  request re-anchors the whole context before it.
- Model switch: every message keeps the value it was read with. The messages
  first read by the new model's first request get their estimated share of that
  request's whole-context input (two tokenizers cannot be subtracted); later
  intervals subtract as usual. A context drawn after the switch (`context_through`)
  re-splits what came before it against the new model's total (estimated).
- Tail after the last request: a sealed segment's closing request (the
  compaction LLM call reading the whole segment, `ClosingRequest`) anchors it.
  Without one the tail was never read by any LLM -- `context_tokens` is None
  ("not in context yet"), as is every message of a segment with no request.

`generation_tokens` is an AIMessage's `output_tokens` (exact), None without usage.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Literal, cast

from langchain_core.messages import AIMessage, BaseMessage, SystemMessage

from base.agents.history.checkpoint import FullHistory
from base.agents.history.closing_request import ClosingRequest
from base.agents.messages.token_estimate import (
    ai_message_texts,
    estimate_message_tokens,
    estimate_text_tokens,
)

TokenSource = Literal["exact", "estimated"]
_SOURCES: tuple[TokenSource, ...] = ("exact", "estimated")
_WEIGHT_SCALE = 1000  # estimate -> integer weight for the exact apportionment


@dataclass(frozen=True)
class MessageTokens:
    """One message's token weight. `context_tokens` is what the message occupies
    in the requests that carry it, or None when no request has read it yet (then
    `source` is None too). `generation_tokens` is set only for an AIMessage with
    usage."""

    context_tokens: int | None
    generation_tokens: int | None
    source: TokenSource | None


@dataclass(frozen=True)
class ModelSwitch:
    """The first request on a new model: body index of its AIMessage and the provider-reported
    input of its whole context, in the new model's tokenizer."""

    idx: int
    input_tokens: int


@dataclass(frozen=True)
class SegmentTokens:
    """Per-message tokens of one compaction segment. `head` is the segment's
    SystemMessage (None when the snapshot had none); `messages` aligns with the
    segment body. `last_input_tokens` is the `input_tokens` of the segment's
    last request that reported usage. `switches` are the model switches inside the
    segment (see `context_through`): each message keeps the value it had when its
    own request read it, and a switch only matters to a context drawn after it."""

    head: MessageTokens | None
    messages: tuple[MessageTokens, ...]
    last_input_tokens: int | None
    switches: tuple[ModelSwitch, ...] = ()


@dataclass(frozen=True)
class TokenTotal:
    """An aggregate of token values (a segment, a Context Breakdown bucket).

    `estimated` is True when ANY counted part is estimated (the frontend appends
    "(estimated)" to the value); `exact_fraction` is the share of `tokens` that
    came from exact parts (1.0 for an empty aggregate). Messages not yet in
    context (None) are not counted."""

    tokens: int
    estimated: bool
    exact_fraction: float


def total_of(records: Sequence[MessageTokens]) -> TokenTotal:
    """Aggregate `context_tokens` of `records` with the exactness rule above."""
    counted = [r for r in records if r.context_tokens is not None]
    tokens = sum(r.context_tokens or 0 for r in counted)
    exact = sum(r.context_tokens or 0 for r in counted if r.source == "exact")
    return TokenTotal(
        tokens=tokens,
        estimated=any(r.source != "exact" for r in counted),
        exact_fraction=exact / tokens if tokens else 1.0,
    )


@dataclass(frozen=True)
class PartTokens:
    """One part of a message (an AIMessage's reasoning / output / tool_call, a
    system-prompt section). How a whole divides among its parts is never
    measured, so a part is `estimated`; a lone part keeps the whole's source."""

    tokens: int
    source: TokenSource


@dataclass(frozen=True)
class SegmentSummary:
    """A segment's totals (head included in `context_tokens`) and how much of
    them each `source` accounts for. `estimated` / `exact_fraction` follow
    `TokenTotal`; `unread_messages` counts messages not yet in any request."""

    message_count: int
    head_tokens: int
    context_tokens: int
    generation_tokens: int
    tokens_by_source: dict[TokenSource, int]
    messages_by_source: dict[TokenSource, int]
    unread_messages: int
    last_input_tokens: int | None
    estimated: bool
    exact_fraction: float


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
    each is `estimated`; a lone part keeps `total_source`."""
    if len(texts) == 1:
        return {name: PartTokens(total, total_source) for name in texts}
    weights = [round(estimate_text_tokens(t) * _WEIGHT_SCALE) for t in texts.values()]
    return {
        name: PartTokens(share, "estimated")
        for name, share in zip(texts, apportion(weights, total), strict=True)
    }


def ai_message_parts(msg: AIMessage, record: MessageTokens) -> dict[str, PartTokens]:
    """An AIMessage's reasoning / output / tool_call split of its `record` (which must be in
    context). Only non-empty parts share the tokens; an empty part is 0, and a message with a
    single non-empty part (or none: its tokens are framing, kept as output) keeps the whole's
    source."""
    if record.context_tokens is None or record.source is None:
        raise ValueError("message is not in context yet; it has no tokens to split")
    texts = ai_message_texts(msg)
    present = {kind: text for kind, text in texts.items() if text}
    if not present:
        present = {"output": ""}
    shared = split_parts(present, record.context_tokens, record.source)
    return {kind: shared.get(kind, PartTokens(0, record.source)) for kind in texts}


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


@dataclass(frozen=True)
class _Anchor:
    """A request whose provider-reported input covers everything before `idx`."""

    idx: int  # body index of the AIMessage (len(body) for a closing request)
    input: int
    out: int
    model: str | None
    inexact: bool = False  # input is provider total minus an estimated extra
    rebase: bool = False  # re-anchors the whole context before it (history was rewritten)
    switch: bool = False  # first request on a new model


def _candidates(body: Sequence[BaseMessage], closing: ClosingRequest | None) -> list[_Anchor]:
    found: list[_Anchor] = []
    for i, msg in enumerate(body):
        inp, out = _usage(msg, "input_tokens"), _usage(msg, "output_tokens")
        if inp is not None and out is not None:
            found.append(_Anchor(i, inp, out, _model_name(cast(AIMessage, msg))))
    if closing is not None and closing.input_tokens - closing.extra_tokens > 0:
        found.append(
            _Anchor(
                len(body),
                closing.input_tokens - closing.extra_tokens,
                0,
                closing.model,
                inexact=closing.extra_tokens > 0,
            )
        )
    return found


def _chain(candidates: Sequence[_Anchor]) -> list[_Anchor]:
    """The consistent anchor chain. A later request that contradicts the last
    anchor (negative remainder) drops that anchor and retries against the one
    before; with none left it re-anchors. A model switch re-anchors outright."""
    chain: list[_Anchor] = []
    for b in candidates:
        while True:
            if not chain:
                chain.append(replace(b, rebase=True))
                break
            a = chain[-1]
            if a.model is not None and b.model is not None and a.model != b.model:
                chain.append(replace(b, switch=True))
                break
            if b.input - a.input - a.out >= 0:
                chain.append(b)
                break
            chain.pop()
    return chain


def _allocate(
    msgs: Sequence[BaseMessage], total: int, *, inexact: bool
) -> list[tuple[int, TokenSource]]:
    """Give `total` provider tokens to `msgs`: whole to a lone message, else by estimate."""
    if len(msgs) == 1 and not inexact:
        return [(total, "exact")]
    weights = [round(estimate_message_tokens(m) * _WEIGHT_SCALE) for m in msgs]
    return [(share, "estimated") for share in apportion(weights, total)]


def _generation(msg: BaseMessage) -> int | None:
    return _usage(msg, "output_tokens")


def _fill(
    out: list[MessageTokens],
    indices: Sequence[int],
    values: Sequence[tuple[int, TokenSource]],
) -> None:
    for i, (ctx, src) in zip(indices, values, strict=True):
        out[i] = MessageTokens(ctx, out[i].generation_tokens, src)


def _fill_prefix(
    out: list[MessageTokens],
    head: SystemMessage | None,
    body: Sequence[BaseMessage],
    first: _Anchor,
) -> MessageTokens | None:
    """Share the first anchor's input among head + the messages before it;
    returns the head's record."""
    members: list[BaseMessage] = ([head] if head is not None else []) + list(body[: first.idx])
    values = _allocate(members, first.input, inexact=first.inexact) if members else []
    head_rec = None
    if head is not None:
        ctx, src = values.pop(0)
        head_rec = MessageTokens(ctx, None, src)
    _fill(out, range(first.idx), values)
    return head_rec


def _prefix_shares(
    head: SystemMessage | None, body: Sequence[BaseMessage], upto: int, total: int
) -> list[int]:
    """`total` shared among head + body[:upto] by the estimator, head first when there is one."""
    members: list[BaseMessage] = ([head] if head is not None else []) + list(body[:upto])
    return apportion([round(estimate_message_tokens(m) * _WEIGHT_SCALE) for m in members], total)


def _fill_interval(
    out: list[MessageTokens],
    head: SystemMessage | None,
    body: Sequence[BaseMessage],
    here: _Anchor,
    there: _Anchor,
) -> None:
    """Share the growth between two anchors among the messages between them. Across a model
    switch the growth is not comparable (two tokenizers): the messages between get their
    estimated share of the new model's whole-context input, and everything before keeps the
    value it was read with."""
    out[here.idx] = MessageTokens(here.out, here.out, "exact")
    between = range(here.idx + 1, there.idx)
    if not between:
        return
    if there.switch:
        shares = _prefix_shares(head, body, there.idx, there.input)
        offset = 1 if head is not None else 0
        _fill(out, between, [(shares[offset + i], "estimated") for i in between])
        return
    total = there.input - here.input - here.out
    _fill(out, between, _allocate([body[i] for i in between], total, inexact=there.inexact))


def segment_tokens(
    head: SystemMessage | None,
    body: Sequence[BaseMessage],
    closing: ClosingRequest | None = None,
) -> SegmentTokens:
    """Per-message tokens of one segment, each as it was read: its head and its body messages.

    `closing` is the request that read the whole segment when it was sealed by a
    compaction LLM call; without it the tail after the last request stays None."""
    last_input = next(
        (u for m in reversed(body) if (u := _usage(m, "input_tokens")) is not None), None
    )
    chain = _chain(_candidates(body, closing))
    out = [MessageTokens(None, _generation(m), None) for m in body]
    if not chain:
        head_rec = MessageTokens(None, None, None) if head is not None else None
        return SegmentTokens(head_rec, tuple(out), last_input)

    start = max(i for i, a in enumerate(chain) if a.rebase)
    head_rec = _fill_prefix(out, head, body, chain[start])
    for here, there in zip(chain[start:], chain[start + 1 :], strict=False):
        _fill_interval(out, head, body, here, there)
    final = chain[-1]
    if final.idx < len(body):
        out[final.idx] = MessageTokens(final.out, final.out, "exact")
    switches = tuple(ModelSwitch(a.idx, a.input) for a in chain[start + 1 :] if a.switch)
    return SegmentTokens(head_rec, tuple(out), last_input, switches)


def context_through(
    head: SystemMessage | None, body: Sequence[BaseMessage], segment: SegmentTokens, upto: int
) -> tuple[MessageTokens | None, list[MessageTokens]]:
    """The head and body[:upto] as the context of the request at body index `upto`.

    Values are the ones each message was read with, except that a model switch inside the
    span re-splits everything before the last switch by the estimator against that request's
    whole-context input (the new tokenizer's total), marked estimated; messages from the
    switch on keep their own values, so the context still sums to the request's input."""
    records = list(segment.messages[:upto])
    head_rec = segment.head
    switch = next((sw for sw in reversed(segment.switches) if sw.idx <= upto), None)
    if switch is None:
        return head_rec, records
    shares = _prefix_shares(head, body, switch.idx, switch.input_tokens)
    if head is not None:
        head_rec = MessageTokens(shares.pop(0), None, "estimated")
    for i, share in enumerate(shares):
        records[i] = MessageTokens(share, records[i].generation_tokens, "estimated")
    return head_rec, records


def history_segment_tokens(history: FullHistory) -> tuple[SegmentTokens, ...]:
    """`segment_tokens` for every segment of a stitched history, in order; a sealed segment's
    closing request (`history.segment_closings`) anchors its tail."""
    starts = history.segment_starts
    ends = [*starts[1:], len(history.messages)][: len(starts)]
    closings = history.segment_closings
    return tuple(
        segment_tokens(
            head,
            history.messages[start:end],
            closings[k] if k < len(closings) else None,
        )
        for k, (head, start, end) in enumerate(
            zip(history.segment_heads, starts, ends, strict=True)
        )
    )


def history_message_tokens(
    history: FullHistory, segments: Sequence[SegmentTokens] | None = None
) -> list[MessageTokens]:
    """One record per `history.messages` entry. The stitched list keeps segment 0's own head as
    its first message (`segment_starts[0] == 1`); it takes that head's record. Pass `segments`
    when already computed."""
    computed = history_segment_tokens(history) if segments is None else segments
    if not computed:
        return []
    lead = history.segment_starts[0]
    head = computed[0].head or MessageTokens(None, None, None)
    return [head] * lead + [m for seg in computed for m in seg.messages]


def summarize_segment(segment: SegmentTokens) -> SegmentSummary:
    """Totals of one segment for list / gauge views."""
    records = ([segment.head] if segment.head is not None else []) + list(segment.messages)
    tokens: dict[TokenSource, int] = dict.fromkeys(_SOURCES, 0)
    counts: dict[TokenSource, int] = dict.fromkeys(_SOURCES, 0)
    unread = 0
    for r in records:
        if r.context_tokens is None or r.source is None:
            unread += 1
            continue
        tokens[r.source] += r.context_tokens
        counts[r.source] += 1
    total = total_of(records)
    return SegmentSummary(
        message_count=len(segment.messages),
        head_tokens=(segment.head.context_tokens or 0) if segment.head is not None else 0,
        context_tokens=total.tokens,
        generation_tokens=sum(r.generation_tokens or 0 for r in records),
        tokens_by_source=tokens,
        messages_by_source=counts,
        unread_messages=unread,
        last_input_tokens=segment.last_input_tokens,
        estimated=total.estimated,
        exact_fraction=total.exact_fraction,
    )


def summarize_segments(history: FullHistory) -> tuple[SegmentSummary, ...]:
    """Per-segment totals of a stitched history."""
    return tuple(summarize_segment(s) for s in history_segment_tokens(history))
