"""Compute a context-window breakdown for one request — pure view logic over the
checkpoint messages and their per-message token counts (`base/agents/history/message_tokens.py`),
gateway-side (zero kernel/agent involvement).

Each message is bucketed by kind (system prompt / cluster+agent memory /
reasoning / output / tool call+response / compact summary / context note, and
inbound messages split by source into user input / agent messages / automation)
using the same discriminators the timeline classifier keys on (`ava_msg_type`,
`ava_note_tag`, the inbound `ava_source`, AIMessage content-block types).

A bucket's tokens are the sum of its messages' own counts, which are anchored to the provider's
reported `input_tokens` (exact for a lone message in a request interval, estimated where several
messages shared one). Only the inside of a message is divided by the estimator: an AIMessage into
reasoning / output / tool_call, the system prompt into a **recursive** section tree (top-level `#`
sections, and any section over `SECTION_SPLIT_THRESHOLD_TOKENS` drilled into its next-level
sub-headings). Every category carries whether any of its parts was estimated and the exact
share of its tokens.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from agent.messages import COMPACT_SUMMARY_HEADER
from base.agents.history.message_tokens import (
    MessageTokens,
    SegmentTokens,
    TokenTotal,
    ai_message_parts,
    apportion,
    context_through,
    segment_tokens,
    total_of,
)
from base.agents.messages.kwargs import AvaMsgType, NoteTag, read_ava_kwargs
from base.agents.messages.token_estimate import estimate_text_tokens

# Bucket kinds — the canonical enumeration, and the stable tie-break order when
# two categories share the same token count (the frontend legend renders them
# sorted by context share, descending). A closed set: an
# untagged HumanMessage that is not the compact summary falls to `user_input`.
# Inbound messages are split by their envelope `ava_source` into three buckets —
# `user_input` (a human turn), `agent_messages` (a peer agent), `automation` (a
# machine/framework wakeup or notice) — see `_inbound_bucket`. The frontend
# legend merges `automation` with `context_note` into one "System notes" row
# (user ruling 2026-08-04); the API keeps the two kinds separate.
CATEGORY_ORDER: tuple[str, ...] = (
    "system_prompt",
    "compact_summary",
    "cluster_memory",
    "agent_memory",
    "context_note",
    "user_input",
    "agent_messages",
    "automation",
    "reasoning",
    "output",
    "tool_call",
    "tool_response",
)


def _inbound_bucket(source: str) -> str:
    """Bucket an inbound HumanMessage by its envelope `ava_source` (the taxonomy
    in `base/agents/messages/envelope.py`): a peer agent (`agent:N`) -> `agent_messages`; a
    machine- or framework-originated wakeup / notice (`watcher:N` / `shell:N` /
    `schedule:N` / `system` / `system:<subtype>`) -> `automation`; everything else
    — a human turn (`user`, `ui:page:<name>`) or a legacy inbound with no recorded
    source — -> `user_input`. Keeping `user_input` the default preserves the old
    single-bucket behavior for any inbound whose source we can't classify."""
    if source.startswith("agent:"):
        return "agent_messages"
    if source == "system" or source.startswith(("system:", "watcher:", "shell:", "schedule:")):
        return "automation"
    return "user_input"


# ava_note_tag values that map to their own bucket; every other note tag
# (agent_id / exec_timeout / compact_reminder / lifecycle_* / ...) is a
# `context_note` (see the system_note branch of `bucket_messages`).


def _note_bucket(tag: object) -> str:
    if tag == NoteTag.MEMORY:
        return "cluster_memory"
    if tag in (NoteTag.AGENT_MEMORY, NoteTag.INHERITED_MEMORY):
        # Inherited-memory blocks are agent memory too (the chain's
        # copy of it), so they share the agent_memory bucket.
        return "agent_memory"
    return "context_note"


def _human_bucket(msg: HumanMessage) -> str:
    """The context bucket a HumanMessage belongs to, by its Ava message type."""
    kwargs = read_ava_kwargs(msg)
    ava_type = kwargs.get("ava_msg_type")
    if ava_type == AvaMsgType.INBOUND:
        return _inbound_bucket(kwargs.get("ava_source") or "")
    if ava_type == AvaMsgType.COMPACT_SUMMARY:
        return "compact_summary"
    if ava_type == AvaMsgType.SYSTEM_NOTE:
        return _note_bucket(kwargs.get("ava_note_tag"))
    if isinstance(msg.content, str) and msg.content.startswith(COMPACT_SUMMARY_HEADER):  # pyright: ignore[reportUnknownMemberType]
        # The auto-compact summary is injected as an untagged HumanMessage
        # (compose_summary_message); the header is its one invariant.
        return "compact_summary"
    return "user_input"


def _parts_for(msg: BaseMessage, record: MessageTokens) -> list[tuple[str, MessageTokens]]:
    """The `(bucket, tokens)` parts one counted message contributes."""
    tokens, source = record.context_tokens, record.source
    assert tokens is not None and source is not None  # noqa: S101
    if isinstance(msg, SystemMessage):
        return [("system_prompt", record)]
    if isinstance(msg, ToolMessage):
        return [("tool_response", record)]
    if isinstance(msg, AIMessage):
        return [
            (kind, MessageTokens(part.tokens, None, part.source))
            for kind, part in ai_message_parts(msg, record).items()
            if part.tokens
        ]
    if isinstance(msg, HumanMessage):
        return [(_human_bucket(msg), record)]
    return []


def bucket_messages(
    messages: Sequence[BaseMessage], records: Sequence[MessageTokens]
) -> dict[str, list[MessageTokens]]:
    """Bucket the messages (aligned with their `records`) into `{kind: [tokens parts]}`.
    A message not yet read by any request (no tokens) is left out; buckets that never occur
    are absent."""
    buckets: dict[str, list[MessageTokens]] = {}
    for msg, record in zip(messages, records, strict=True):
        if record.context_tokens is None:
            continue
        for kind, part in _parts_for(msg, record):
            buckets.setdefault(kind, []).append(part)
    return buckets


# A system-prompt section is drilled into its next-level sub-headings only when
# its normalized estimate exceeds this many tokens; at or below it (or with no
# deeper heading) it stays a leaf. The top-level `#` sections are always listed
# — the threshold governs recursion *into* a section, not whether the section
# list itself is shown.
SECTION_SPLIT_THRESHOLD_TOKENS = 1000


@dataclass
class SectionNode:
    """One node of the recursive system-prompt breakdown: a heading — or the
    `(preamble)` / `(intro)` prose that precedes the first sub-heading — with its
    normalized token estimate and, when it was large enough to split, its
    children. When `children` is non-empty their tokens sum exactly to `tokens`
    (the residual prose is itself surfaced as a leaf child), so the tree conserves
    the parent's tokens at every level."""

    name: str
    tokens: int
    children: list[SectionNode] = field(default_factory=list)
    estimated: bool = True  # a section is a share of the system prompt, never measured alone


@dataclass
class _WeightNode:
    """Structural section tree in estimator weight, before token apportionment/pruning.
    `own_weight` = the node's own residual prose (its heading line plus the text up
    to its first sub-heading); `children` = its sub-sections."""

    name: str
    own_weight: int
    children: list[_WeightNode] = field(default_factory=list)

    @property
    def total_weight(self) -> int:
        return self.own_weight + sum(c.total_weight for c in self.children)


def _heading_level(line: str) -> int | None:
    """Markdown heading level of `line` (1-6, `#`..`######` followed by a space),
    or None when the line is not an ATX heading."""
    if not line.startswith("#"):
        return None
    hashes = len(line) - len(line.lstrip("#"))
    if 1 <= hashes <= 6 and line[hashes : hashes + 1] == " ":
        return hashes
    return None


def _split_children(lines: list[tuple[str, int]], level: int) -> tuple[int, list[_WeightNode]]:
    """Partition `lines` (each `(text, weight)`) at level-`level` headings. Returns
    `(own_weight, children)`: `own_weight` is the run before the first level-`level`
    heading (this node's residual prose), and each subsequent level-`level` heading
    opens a child, recursively split at `level + 1`."""
    own = 0
    idx = 0
    n = len(lines)
    while idx < n and _heading_level(lines[idx][0]) != level:
        own += lines[idx][1]
        idx += 1
    children: list[_WeightNode] = []
    while idx < n:
        head_line, _ = lines[idx]
        name = head_line[level:].strip() or "(untitled)"
        seg = [lines[idx]]
        idx += 1
        while idx < n and _heading_level(lines[idx][0]) != level:
            seg.append(lines[idx])
            idx += 1
        seg_own, seg_children = _split_children(seg, level + 1)
        children.append(_WeightNode(name=name, own_weight=seg_own, children=seg_children))
    return own, children


def _build_weight_tree(content: str) -> _WeightNode:
    """The full structural section tree (every heading level), in estimator weight.
    The root carries the pre-first-`#` preamble as its `own_weight`; each line's weight
    (its text plus the newline) lands in exactly one node, so the leaves
    partition the whole content."""
    lines = [(line, _weight(line + "\n")) for line in content.split("\n")]
    own, children = _split_children(lines, 1)
    return _WeightNode(name="", own_weight=own, children=children)


def _items_of(node: _WeightNode, residual_name: str) -> list[tuple[str, int, _WeightNode | None]]:
    """The node's children as apportionment items `(name, weight, child_or_None)`:
    the residual prose first (a leaf, `child_or_None is None`) when non-empty, then
    each sub-section."""
    items: list[tuple[str, int, _WeightNode | None]] = []
    if node.own_weight > 0:
        items.append((residual_name, node.own_weight, None))
    for c in node.children:
        items.append((c.name, c.total_weight, c))
    return items


def _distribute(items: list[tuple[str, int, _WeightNode | None]], budget: int) -> list[SectionNode]:
    """Apportion `budget` tokens across `items` proportional to their estimator weight (exactly
    conserving it), building each into a `SectionNode`. A sub-section item recurses only when it
    has deeper headings *and* its share exceeds the split threshold; otherwise it — and every
    residual/leaf item — becomes a leaf carrying its share."""
    toks = apportion([weight for _, weight, _ in items], budget)
    out: list[SectionNode] = []
    for (name, _, child), t in zip(items, toks, strict=True):
        if child is not None and child.children and t > SECTION_SPLIT_THRESHOLD_TOKENS:
            out.append(
                SectionNode(
                    name=name, tokens=t, children=_distribute(_items_of(child, "(intro)"), t)
                )
            )
        else:
            out.append(SectionNode(name=name, tokens=t))
    return out


def _weight(text: str) -> int:
    return round(estimate_text_tokens(text) * 1000)


def section_breakdown(content: str, system_prompt_tokens: int) -> list[SectionNode]:
    """Split the system prompt into a recursive section tree whose top-level nodes sum to
    `system_prompt_tokens` and each parent's tokens split among its children. Any section over
    `SECTION_SPLIT_THRESHOLD_TOKENS` is drilled into its sub-headings, recursively. Empty input
    or no tokens -> empty list."""
    if not content or system_prompt_tokens <= 0:
        return []
    root = _build_weight_tree(content)
    if root.total_weight <= 0:
        return []
    return _distribute(_items_of(root, "(preamble)"), system_prompt_tokens)


@dataclass(frozen=True)
class CategoryTotal:
    """One category of the breakdown: its tokens and whether any part was estimated."""

    kind: str
    total: TokenTotal


@dataclass(frozen=True)
class RequestBreakdown:
    """What one request's input held: `categories` in CATEGORY_ORDER (only present kinds), the
    system-prompt `sections`, and the `total` of everything counted."""

    categories: list[CategoryTotal]
    sections: list[SectionNode]
    total: TokenTotal


def compute_breakdown(
    messages: Sequence[BaseMessage], records: Sequence[MessageTokens]
) -> RequestBreakdown:
    """The breakdown of `messages` (a request's input, its head SystemMessage first when it had
    one) from their per-message token counts `records`."""
    buckets = bucket_messages(messages, records)
    categories = [CategoryTotal(k, total_of(buckets[k])) for k in CATEGORY_ORDER if k in buckets]
    system_prompt = next((m for m in messages if isinstance(m, SystemMessage)), None)
    system_tokens = next((c.total.tokens for c in categories if c.kind == "system_prompt"), 0)
    sections = (
        section_breakdown(_text(system_prompt.content), system_tokens)  # pyright: ignore[reportUnknownMemberType]
        if system_prompt is not None
        else []
    )
    total = total_of([part for parts in buckets.values() for part in parts])
    return RequestBreakdown(categories, sections, total)


def request_breakdown(
    head: SystemMessage | None, body: Sequence[BaseMessage], segment: SegmentTokens, upto: int
) -> RequestBreakdown:
    """The breakdown of the request at body index `upto`: the segment's head and `body[:upto]`,
    as that request's context (a model switch before it re-splits what preceded it).

    The request's own `input_tokens` is the provider's number: when the parts add up to it, the
    total is exact however the parts were obtained (the categories stay as they are)."""
    head_rec, records = context_through(head, body, segment, upto)
    messages: list[BaseMessage] = ([head] if head is not None else []) + list(body[:upto])
    counted = ([head_rec] if head is not None and head_rec is not None else []) + records
    found = compute_breakdown(messages, counted)
    request = body[upto]
    reported = (
        request.usage_metadata["input_tokens"]
        if isinstance(request, AIMessage) and request.usage_metadata
        else None
    )
    if reported == found.total.tokens:
        found = replace(
            found, total=TokenTotal(tokens=reported, estimated=False, exact_fraction=1.0)
        )
    return found


def latest_request_breakdown(messages: Sequence[BaseMessage]) -> RequestBreakdown:
    """The breakdown of the latest request of one snapshot (`messages` = head + conversation,
    the open segment). Empty when no request has reported usage yet."""
    head = messages[0] if messages and isinstance(messages[0], SystemMessage) else None
    body = list(messages[1:] if head is not None else messages)
    segment = segment_tokens(head, body)
    upto: int | None = None
    for i in range(len(body) - 1, -1, -1):
        candidate = body[i]
        if isinstance(candidate, AIMessage) and candidate.usage_metadata:
            upto = i
            break
    if upto is None:
        return RequestBreakdown([], [], total_of([]))
    return request_breakdown(head, body, segment, upto)


def _text(content: object) -> str:
    return content if isinstance(content, str) else str(content)
