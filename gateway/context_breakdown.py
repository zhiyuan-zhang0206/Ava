"""Compute a context-window breakdown for one agent — pure view logic over the
checkpoint messages, gateway-side (zero kernel/agent involvement).

Each message is bucketed by kind (system prompt / cluster+agent memory /
reasoning / output / tool call+response / compact summary / context note, and
inbound messages split by source into user input / agent messages / automation)
using the same discriminators the timeline classifier keys on (`ava_msg_type`,
`ava_note_tag`, the inbound `ava_source`, AIMessage content-block types) — but
counted over the *raw* content chars the model actually sees, not the rendered
timeline payload. The system prompt is additionally split into a **recursive** section
tree: top-level `#` sections, and any section whose estimate exceeds
`SECTION_SPLIT_THRESHOLD_TOKENS` is drilled into its next-level sub-headings, on
down until every leaf is at or below the threshold or has no deeper heading to
split (e.g. an "expanded SDK reference" section that dwarfs everything else).

Every bucket is a chars/4 estimate, then **proportionally normalized** to the
last LLM call's real `input_tokens` (`shared/lm/context_budget.latest_input_tokens`)
so the parts sum exactly to the provider-truth total — the total is exact, the
distribution is approximate, at zero extra API cost and zero KV-cache impact.
The section tree is apportioned the same way (each parent's tokens split among
its children), so it conserves the parent's tokens at every level.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from agent.messages import COMPACT_SUMMARY_HEADER
from shared.lm.content import content_blocks
from shared.message_kwargs import AvaMsgType, NoteTag, read_ava_kwargs

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
    in `shared/envelope.py`): a peer agent (`agent:N`) -> `agent_messages`; a
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


def _text_chars(content: object) -> int:
    """Char count of a message's renderable text. A block list (multimodal
    inbound, AIMessage content blocks) counts only its text/thinking text — never
    the base64 of an image block (that becomes image tokens, not char tokens, and
    would wildly inflate a chars/4 estimate)."""
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for b in content_blocks(cast(list[Any], content)):
            if isinstance(b, dict):
                d = cast(dict[str, Any], b)
                if isinstance(text := d.get("text"), str):
                    total += len(text)
                elif isinstance(thinking := d.get("thinking"), str):
                    total += len(thinking)
            elif isinstance(b, str):
                total += len(b)
        return total
    return len(str(content))


def _ai_message_chars(msg: AIMessage) -> dict[str, int]:
    """Split one AIMessage's chars into reasoning / output / tool_call buckets."""
    out = {"reasoning": 0, "output": 0, "tool_call": 0}
    content: Any = msg.content  # pyright: ignore[reportUnknownMemberType]
    if isinstance(content, str):
        out["output"] += len(content)
    elif isinstance(content, list):
        for b in content_blocks(cast(list[Any], content)):
            if not isinstance(b, dict):
                continue
            d = cast(dict[str, Any], b)
            if isinstance(thinking := d.get("thinking"), str):
                out["reasoning"] += len(thinking)
            elif b.get("type") == "text" and isinstance(text := b.get("text"), str):
                out["output"] += len(text)
    for tc in msg.tool_calls:
        if isinstance(code := tc["args"].get("code"), str):
            out["tool_call"] += len(code)
    return out


def bucket_messages(messages: Sequence[BaseMessage]) -> tuple[dict[str, int], str]:
    """Bucket the raw messages into `{kind: chars}` and return the system-prompt
    content alongside (for the section split). Buckets that never occur are
    absent from the dict."""
    buckets: dict[str, int] = {}
    system_prompt_content = ""

    def add(kind: str, chars: int) -> None:
        if chars:
            buckets[kind] = buckets.get(kind, 0) + chars

    for msg in messages:
        if isinstance(msg, SystemMessage):
            text = msg.content if isinstance(msg.content, str) else str(msg.content)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            system_prompt_content = text
            add("system_prompt", len(text))
            continue
        if isinstance(msg, ToolMessage):
            add("tool_response", _text_chars(msg.content))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
            continue
        if isinstance(msg, AIMessage):
            for kind, chars in _ai_message_chars(msg).items():
                add(kind, chars)
            continue
        if isinstance(msg, HumanMessage):
            kwargs = read_ava_kwargs(msg)
            ava_type = kwargs.get("ava_msg_type")
            chars = _text_chars(msg.content)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
            if ava_type == AvaMsgType.INBOUND:
                add(_inbound_bucket(kwargs.get("ava_source") or ""), chars)
            elif ava_type == AvaMsgType.COMPACT_SUMMARY:
                add("compact_summary", chars)
            elif ava_type == AvaMsgType.SYSTEM_NOTE:
                tag = kwargs.get("ava_note_tag")
                if tag == NoteTag.MEMORY:
                    add("cluster_memory", chars)
                elif tag == NoteTag.AGENT_MEMORY:
                    add("agent_memory", chars)
                else:
                    add("context_note", chars)
            elif isinstance(msg.content, str) and msg.content.startswith(COMPACT_SUMMARY_HEADER):  # pyright: ignore[reportUnknownMemberType]
                # The auto-compact summary is injected as an untagged HumanMessage
                # (compose_summary_message); the header is its one invariant.
                add("compact_summary", chars)
            else:
                add("user_input", chars)
    return buckets, system_prompt_content


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


@dataclass
class _CharNode:
    """Structural section tree in raw chars, before token apportionment/pruning.
    `own_chars` = the node's own residual prose (its heading line plus the text up
    to its first sub-heading); `children` = its sub-sections."""

    name: str
    own_chars: int
    children: list[_CharNode] = field(default_factory=list)

    @property
    def total_chars(self) -> int:
        return self.own_chars + sum(c.total_chars for c in self.children)


def _heading_level(line: str) -> int | None:
    """Markdown heading level of `line` (1-6, `#`..`######` followed by a space),
    or None when the line is not an ATX heading."""
    if not line.startswith("#"):
        return None
    hashes = len(line) - len(line.lstrip("#"))
    if 1 <= hashes <= 6 and line[hashes : hashes + 1] == " ":
        return hashes
    return None


def _split_children(lines: list[tuple[str, int]], level: int) -> tuple[int, list[_CharNode]]:
    """Partition `lines` (each `(text, chars)`) at level-`level` headings. Returns
    `(own_chars, children)`: `own_chars` is the run before the first level-`level`
    heading (this node's residual prose), and each subsequent level-`level` heading
    opens a child, recursively split at `level + 1`."""
    own = 0
    idx = 0
    n = len(lines)
    while idx < n and _heading_level(lines[idx][0]) != level:
        own += lines[idx][1]
        idx += 1
    children: list[_CharNode] = []
    while idx < n:
        head_line, _ = lines[idx]
        name = head_line[level:].strip() or "(untitled)"
        seg = [lines[idx]]
        idx += 1
        while idx < n and _heading_level(lines[idx][0]) != level:
            seg.append(lines[idx])
            idx += 1
        seg_own, seg_children = _split_children(seg, level + 1)
        children.append(_CharNode(name=name, own_chars=seg_own, children=seg_children))
    return own, children


def _build_char_tree(content: str) -> _CharNode:
    """The full structural section tree (every heading level), in raw chars. The
    root carries the pre-first-`#` preamble as its `own_chars`; each line's chars
    (`len(line) + 1` for the newline) land in exactly one node, so the leaves
    partition the whole content."""
    lines = [(line, len(line) + 1) for line in content.split("\n")]
    own, children = _split_children(lines, 1)
    return _CharNode(name="", own_chars=own, children=children)


def _items_of(node: _CharNode, residual_name: str) -> list[tuple[str, int, _CharNode | None]]:
    """The node's children as apportionment items `(name, chars, child_or_None)`:
    the residual prose first (a leaf, `child_or_None is None`) when non-empty, then
    each sub-section."""
    items: list[tuple[str, int, _CharNode | None]] = []
    if node.own_chars > 0:
        items.append((residual_name, node.own_chars, None))
    for c in node.children:
        items.append((c.name, c.total_chars, c))
    return items


def _distribute(items: list[tuple[str, int, _CharNode | None]], budget: int) -> list[SectionNode]:
    """Apportion `budget` tokens across `items` proportional to their chars (the
    largest item absorbs the rounding residual, so the parts sum exactly to
    `budget`), building each into a `SectionNode`. A sub-section item recurses only
    when it has deeper headings *and* its share exceeds the split threshold;
    otherwise it — and every residual/leaf item — becomes a leaf carrying its share."""
    total = sum(chars for _, chars, _ in items)
    if total <= 0:
        return [SectionNode(name=name, tokens=0) for name, _, _ in items]
    toks = [round(budget * chars / total) for _, chars, _ in items]
    biggest = max(range(len(items)), key=lambda i: items[i][1])
    toks[biggest] += budget - sum(toks)
    out: list[SectionNode] = []
    for (name, _chars, child), t in zip(items, toks, strict=True):
        if child is not None and child.children and t > SECTION_SPLIT_THRESHOLD_TOKENS:
            out.append(
                SectionNode(
                    name=name, tokens=t, children=_distribute(_items_of(child, "(intro)"), t)
                )
            )
        else:
            out.append(SectionNode(name=name, tokens=t))
    return out


def section_breakdown(content: str, system_prompt_tokens: int) -> list[SectionNode]:
    """Split the system prompt into a recursive section tree, normalized so the
    top-level nodes sum to `system_prompt_tokens` (the system_prompt category's
    normalized tokens) and each parent's tokens split among its children. Any
    section over `SECTION_SPLIT_THRESHOLD_TOKENS` is drilled into its sub-headings,
    recursively. With no anchor (`system_prompt_tokens <= 0`) the values fall back
    to the raw chars/4 estimate (apportioned identically, so still conserved).
    Empty input -> empty list."""
    if not content:
        return []
    root = _build_char_tree(content)
    total_chars = root.total_chars
    if total_chars <= 0:
        return []
    budget = system_prompt_tokens if system_prompt_tokens > 0 else total_chars // 4
    return _distribute(_items_of(root, "(preamble)"), budget)


def normalize(buckets: dict[str, int], total_target: int) -> dict[str, int]:
    """Scale `{k: chars}` so the values sum exactly to `total_target` tokens,
    preserving proportions (largest bucket absorbs the rounding residual). When
    there is no anchor yet (`total_target <= 0`) or the input is empty, fall back
    to the raw chars/4 estimate."""
    est_total = sum(buckets.values())
    if total_target <= 0 or est_total <= 0:
        return {k: v // 4 for k, v in buckets.items()}
    rounded = {k: round(v * total_target / est_total) for k, v in buckets.items()}
    residual = total_target - sum(rounded.values())
    if rounded:
        biggest = max(buckets, key=lambda k: buckets[k])
        rounded[biggest] += residual
    return rounded


def compute_breakdown(
    messages: Sequence[BaseMessage], total_input_tokens: int
) -> tuple[list[tuple[str, int]], list[SectionNode], int]:
    """Return `(categories, sections, estimated_total)`.

    `categories` = `[(kind, tokens)]` in CATEGORY_ORDER (only present kinds),
    normalized to `total_input_tokens`. `sections` = the recursive system-prompt
    sub-split (`SectionNode` tree), normalized to the same anchor so its top-level
    nodes sum to the system_prompt category and each parent's tokens split among
    its children. `estimated_total` = the raw chars/4 sum (reconciliation against
    the truth)."""
    buckets, system_prompt_content = bucket_messages(messages)
    estimated_total = sum(buckets.values()) // 4

    norm_categories = normalize(buckets, total_input_tokens)
    categories = [(k, norm_categories[k]) for k in CATEGORY_ORDER if k in norm_categories]

    system_prompt_tokens = norm_categories.get("system_prompt", 0)
    sections = section_breakdown(system_prompt_content, system_prompt_tokens)

    return categories, sections, estimated_total
