"""Layer 0 of the understanding tree — the deterministic message units.

An agent's message history is divided into units by a fixed rule, no model
involved. Every layer above (the chunk summaries, the groups of groups) is
built over these units, and the run-timeline page draws them as its bottom row:

- **work** — one turn's reasoning, its tool calls and the tool results that
  answer them. A new AIMessage that carries reasoning or a tool call opens a
  new work unit, so a unit is one think -> act -> observe step.
- **text** — the agent's text output. Its own unit even when it sits in the
  same AIMessage as a tool call: it may be the agent replying to a human while
  it keeps working, which is not the same thing as reasoning.
- **inbound** — one unit per inbound message.
- **note** — a framework-injected message (system note, attachment, compact
  summary or request). Not part of the three rules above: it is kept as a unit
  of its own so every message of the history sits in at least one unit and the
  raw view of a span never has a hole.

The agent's system prompt (message 0 of a segment) belongs to no unit.

`i0` / `i1` are inclusive indices into the message list given to
`divide_units` — for the stitched checkpoint history, the same indices the
understanding nodes store as their span. Text and work units that come from
one AIMessage share its index; the text unit is listed first. A note or an
attachment between a tool call and its result does not break the work unit.
A tool result with no open call (history cut mid-step) forms a work unit of
its own.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from base.agents.history.timeline import TimelineItem, build_timeline_items
from base.agents.history.timeline_inputs import TimelineReadInputs
from base.agents.messages.kwargs import AvaMsgType, message_addl_kwargs, read_ava_kwargs

UnitKind = Literal["work", "text", "inbound", "note"]

# A message stamped before this carries a synthesized epoch-anchored time (it
# predates `ava_created_at`); such a time cannot be placed on a real axis.
_LEGACY_TS_FLOOR = datetime(2020, 1, 1, tzinfo=UTC)

PREVIEW_CHARS = 160

_AGENT_KINDS = frozenset({"agent_reasoning", "agent_chat", "agent_code"})
_NOTE_KINDS = frozenset({"system_marker", "attach"})
_COMPACT_KINDS = frozenset({"inbound_compact_summary", "inbound_compact_request"})


@dataclass(frozen=True)
class MessageUnit:
    """One layer-0 unit: an inclusive message span, its kind and its wall-clock extent."""

    kind: UnitKind
    i0: int
    i1: int
    start: datetime | None
    end: datetime | None
    source: str | None
    preview: str


def _stamp(item: TimelineItem) -> datetime | None:
    if not item.created_at:
        return None
    ts = datetime.fromisoformat(item.created_at)
    return ts if ts >= _LEGACY_TS_FLOOR else None


def _preview(items: Sequence[TimelineItem]) -> str:
    """The first non-empty payload of the unit, whitespace collapsed and clipped."""
    for item in items:
        text = " ".join(item.payload.split())
        if text:
            return text if len(text) <= PREVIEW_CHARS else text[: PREVIEW_CHARS - 1] + "…"
    return ""


def _unit(
    kind: UnitKind, i0: int, i1: int, items: Sequence[TimelineItem], source: str | None = None
) -> MessageUnit:
    stamps = [ts for item in items if (ts := _stamp(item)) is not None]
    return MessageUnit(
        kind=kind,
        i0=i0,
        i1=i1,
        start=min(stamps) if stamps else None,
        end=max(stamps) if stamps else None,
        source=source,
        preview=_preview(items),
    )


def _by_message(items: Sequence[TimelineItem]) -> list[tuple[int, list[TimelineItem]]]:
    """The items grouped per message index, in message order."""
    groups: list[tuple[int, list[TimelineItem]]] = []
    for item in items:
        idx = int(item.item_id.split(".")[0])
        if groups and groups[-1][0] == idx:
            groups[-1][1].append(item)
        else:
            groups.append((idx, [item]))
    return groups


def read_times(
    messages: Sequence[BaseMessage], *, timeline_inputs: TimelineReadInputs
) -> list[datetime | None]:
    """The time each message was read by the model, per message in message order.

    A message's own time is `message_read_time` (`ava_picked_up_at` when it records one, else
    `ava_created_at`); the timeline items carry it as `created_at`. A message is never read before
    the one before it, so the read time is the running maximum of those times. Data that records
    the pickup is monotone and passes through unchanged; older data, whose inbound messages are
    stamped on arrival (while the agent was still streaming the AIMessage before them), gets its
    read order back, so units and nodes do not overlap on the time axis. A message without a time
    takes the previous value, None until the first. Stored stamps are not touched.
    """
    items, _ = build_timeline_items(messages, [], inputs=timeline_inputs)
    own: list[datetime | None] = [None] * len(messages)
    for idx, group in _by_message(items):
        stamps = [ts for item in group if (ts := _stamp(item)) is not None]
        own[idx] = min(stamps) if stamps else None
    read: list[datetime | None] = []
    latest: datetime | None = None
    for ts in own:
        if ts is not None and (latest is None or ts > latest):
            latest = ts
        read.append(latest)
    return read


def read_unit(unit: MessageUnit, read: Sequence[datetime | None]) -> MessageUnit:
    """`unit` on the read times of its first and last message (a unit with no time stays so)."""
    end = read[unit.i1]
    if unit.start is None or unit.end is None or end is None:
        return unit
    return replace(unit, start=read[unit.i0] or end, end=end)


@dataclass
class _OpenWork:
    i0: int
    i1: int
    items: list[TimelineItem]


class _Divider:
    """Folds the per-message item groups into units, holding the one open work unit."""

    def __init__(self) -> None:
        self.units: list[MessageUnit] = []
        self._work: _OpenWork | None = None

    def close_work(self) -> None:
        if self._work is not None:
            self.units.append(_unit("work", self._work.i0, self._work.i1, self._work.items))
            self._work = None

    def agent_message(self, idx: int, group: list[TimelineItem]) -> None:
        self.close_work()
        text = [item for item in group if item.kind == "agent_chat"]
        steps = [item for item in group if item.kind != "agent_chat"]
        if text:
            self.units.append(_unit("text", idx, idx, text))
        if steps:
            self._work = _OpenWork(idx, idx, steps)

    def tool_result(self, idx: int, group: list[TimelineItem]) -> None:
        if self._work is None:
            self._work = _OpenWork(idx, idx, [])
        self._work.i1 = idx
        self._work.items.extend(group)

    def standalone(
        self, kind: UnitKind, idx: int, group: list[TimelineItem], *, closes: bool
    ) -> None:
        if closes:
            self.close_work()
        self.units.append(_unit(kind, idx, idx, group, group[0].source))

    def message(self, idx: int, group: list[TimelineItem]) -> None:
        kinds = {item.kind for item in group}
        if kinds == {"system_prompt"}:
            return
        if kinds <= _AGENT_KINDS:
            self.agent_message(idx, group)
        elif kinds == {"code_output"}:
            self.tool_result(idx, group)
        elif kinds == {"inbound_chat"}:
            self.standalone("inbound", idx, group, closes=True)
        elif kinds <= _COMPACT_KINDS:
            self.standalone("note", idx, group, closes=True)
        elif kinds <= _NOTE_KINDS:
            self.standalone("note", idx, group, closes=False)
        else:
            raise ValueError(f"message {idx} renders as an uncovered mix of kinds {sorted(kinds)}")


def divide_units(
    messages: Sequence[BaseMessage], *, timeline_inputs: TimelineReadInputs
) -> list[MessageUnit]:
    """Divide a message list into layer-0 units, ordered by first message index.

    Raises:
        ValueError: a message renders as a mix of item kinds no rule covers.
    """
    items, _ = build_timeline_items(messages, [], inputs=timeline_inputs)
    divider = _Divider()
    for idx, group in _by_message(items):
        divider.message(idx, group)
    divider.close_work()
    return sorted(divider.units, key=lambda unit: unit.i0)


# Characters of an inbound / text unit's content in a catalog line, and of each part (reasoning,
# call, output) of a work unit's line.
CONTENT_CHARS = 100
PART_CHARS = 60

_IMPORT_LINE = re.compile(r"^\s*(import\s|from\s+\S+\s+import\s)")


def _code_start(code: str) -> str:
    """`code` without its import lines: the agent's calls mostly open with the same imports,
    so the first line that differs is what tells two calls apart. All-import code stays whole."""
    kept = [line for line in code.splitlines() if not _IMPORT_LINE.match(line)]
    return "\n".join(kept) if any(line.strip() for line in kept) else code


def note_label(msg: BaseMessage) -> str:
    """A short label for a framework-injected message, by its type: `(memory)`,
    `(compact summary)`, `(attachment)`, `(system note)`, a note's tag otherwise
    (`(sdk hint)`, `(task)`, ...)."""
    kwargs = read_ava_kwargs(msg)
    kind = kwargs.get("ava_msg_type")
    if kind == AvaMsgType.COMPACT_SUMMARY:
        return "(compact summary)"
    if kind == AvaMsgType.COMPACT_REQUEST:
        return "(compact request)"
    if kind == AvaMsgType.ATTACH:
        return "(attachment)"
    tag = kwargs.get("ava_note_tag")
    return (
        f"({str(tag).replace('_', ' ')})"
        if kind == AvaMsgType.SYSTEM_NOTE and tag
        else "(system note)"
    )


def _flat(text: str, chars: int) -> str:
    return " ".join(text.split())[:chars]


def inbound_type(source: str | None) -> str:
    """The type label of an inbound unit, from its source: `human message` for the user,
    `agent N message` for agent N, any other source (`watcher:1955`, `shell:1940`,
    `system:notice-reply`) by its own name."""
    if source is None or source == "user":
        return "human message"
    if source.startswith("agent:"):
        return f"agent {source.partition(':')[2]} message"
    return source.replace(":", " ")


def _inbound_content(msg: BaseMessage) -> str:
    """An inbound's content without its envelope header, cut where the writer recorded
    the content to start (`ava_inbound_body_start`; the sender is the unit's source).

    Legacy boundary: inbounds written before that field existed carry no offset, and the
    header is not parsed out of their text; they show whole, header included. Every new
    inbound records the offset, so this branch only ever sees historical messages."""
    start = read_ava_kwargs(msg).get("ava_inbound_body_start")
    return msg.text if start is None else msg.text[start:]


def _result_body(msg: ToolMessage) -> str:
    """A tool result's output without its envelope header, cut where the writer recorded
    the body to start (`ava_exec_body_start`).

    Legacy boundary: results written before that field existed carry no offset, and the
    header is not parsed out of their text; they show whole, header line included. Every
    new exec result records the offset, so this branch only ever sees historical messages."""
    start = read_ava_kwargs(msg).get("ava_exec_body_start")
    return msg.text if start is None else msg.text[start:]


def _output_part(messages: Sequence[BaseMessage], unit: MessageUnit) -> str | None:
    """The start of a work unit's tool output; None for no output or an empty one."""
    for msg in messages[unit.i0 : unit.i1 + 1]:
        if isinstance(msg, ToolMessage):
            return _flat(_result_body(msg), PART_CHARS) or None
    return None


def catalog_line(unit: MessageUnit, messages: Sequence[BaseMessage]) -> str:
    """A unit's catalog text, `type: content`, deterministic and short enough to scan.

    - **inbound**: the sender as the type (`human message`, `agent N message`, `watcher N`, ...)
      and the content without its sender / time line;
    - **text**: `agent text:` and the text;
    - **work**: `work:` then its parts in order, each shortened, separated by ` | ` and named:
      `reasoning[…]`, `call[…]` (the first code line that is not an import) and
      `output[…]`; a part the unit does
      not have, or an empty output, is left out;
    - **note**: a label in parentheses (`note_label`), nothing else.

    `messages` is the list `divide_units` was given.
    """
    msg = messages[unit.i0]
    if unit.kind == "note":
        return note_label(msg)
    if unit.kind == "inbound":
        return f"{inbound_type(unit.source)}: {_flat(_inbound_content(msg), CONTENT_CHARS)}"
    if unit.kind == "text":
        return f"agent text: {_flat(msg.text, CONTENT_CHARS)}"
    parts: list[str] = []
    if isinstance(msg, AIMessage):
        content = msg.content
        thinking = (
            "".join(str(b.get("thinking", "")) for b in content if isinstance(b, dict))
            if isinstance(content, list)
            else ""
        )
        if thinking.strip():
            parts.append(f"reasoning\u300c{_flat(thinking, PART_CHARS)}\u300d")
        if msg.tool_calls:
            args = msg.tool_calls[0]["args"]
            code = args.get("code")
            shown = _code_start(code) if isinstance(code, str) else json.dumps(args)
            parts.append(f"call\u300c{_flat(shown, PART_CHARS)}\u300d")
    output = _output_part(messages, unit)
    if output:
        parts.append(f"output\u300c{output}\u300d")
    return "work: " + " | ".join(parts) if parts else "work:"


# ── The timeline's blocks ──
#
# The blocks the run timeline draws on layer 0: message units, a turn split into its parts.
#
# Grouping and the catalog treat a work unit (reasoning, tool call, result) as one unit; the timeline
# shows it as three blocks on the read times of its messages (`units.read_times`):
#
# - **thinking**: the model's generation for the turn, from the request (the read time of the message
#   before the AIMessage) to the end of the stream (the AIMessage's own time);
# - **call**: the tool call, an instant at the end of the stream;
# - **output**: the execution, from the end of the stream to the tool result.
#
# When the AIMessage also carries text and its reasoning time is recorded (`ava_reasoning_ms_by_block`,
# or the legacy `ava_reasoning_ms`), the thinking block ends after that time and the text block takes
# the rest of the stream; without the record the text is an instant at the end, like the call. A turn
# that is only text spans its stream. Inbound messages and notes are their own blocks, as units.
# Blocks without a time are not produced (legacy messages carry none).

BlockKind = Literal["inbound", "text", "note", "thinking", "call", "output"]


@dataclass(frozen=True)
class DisplayBlock:
    """One block of the run timeline: a message span, its kind and its extent on read times."""

    kind: BlockKind
    i0: int
    i1: int
    start: datetime
    end: datetime
    source: str | None
    preview: str


def _thinking(msg: AIMessage) -> str:
    content = msg.content
    if not isinstance(content, list):
        return ""
    return "".join(str(b.get("thinking", "")) for b in content if isinstance(b, dict))


def _reasoning_ms(msg: AIMessage) -> int | None:
    kwargs = message_addl_kwargs(msg)
    by_block = kwargs.get("ava_reasoning_ms_by_block")
    if isinstance(by_block, dict) and by_block:
        return sum(int(ms) for ms in cast("dict[str, int]", by_block).values())
    legacy = kwargs.get("ava_reasoning_ms")
    return int(legacy) if isinstance(legacy, int | float) else None


def _call_preview(msg: AIMessage) -> str:
    args = msg.tool_calls[0]["args"]
    code = args.get("code")
    return _flat(code if isinstance(code, str) else str(args), PREVIEW_CHARS)


def _turn_blocks(
    idx: int, msg: AIMessage, read: Sequence[datetime | None]
) -> dict[Literal["thinking", "text", "call"], DisplayBlock]:
    """The parts of one AIMessage's turn on read times; empty when the turn has no time."""
    end = read[idx]
    if end is None:
        return {}
    before = read[idx - 1] if idx > 0 else None
    begin = before if before is not None else end
    thinking, text = _thinking(msg).strip(), msg.text.strip()

    def block(kind: BlockKind, start: datetime, stop: datetime, preview: str) -> DisplayBlock:
        return DisplayBlock(kind, idx, idx, start, stop, None, preview)

    out: dict[Literal["thinking", "text", "call"], DisplayBlock] = {}
    split = end
    ms = _reasoning_ms(msg)
    if thinking and text and ms is not None:
        split = min(begin + timedelta(milliseconds=ms), end)
    if thinking:
        out["thinking"] = block("thinking", begin, split, _flat(thinking, PREVIEW_CHARS))
    if text:
        # With reasoning before it the text starts where the reasoning ended (an instant at the
        # end when that time is not recorded); a turn that is only text spans its stream.
        out["text"] = block("text", split if thinking else begin, end, _flat(text, PREVIEW_CHARS))
    if msg.tool_calls:
        out["call"] = block("call", end, end, _call_preview(msg))
    return out


def _output_block(
    unit: MessageUnit, messages: Sequence[BaseMessage], read: Sequence[datetime | None]
) -> DisplayBlock | None:
    first = unit.i0 + 1 if isinstance(messages[unit.i0], AIMessage) else unit.i0
    results = [i for i in range(first, unit.i1 + 1) if isinstance(messages[i], ToolMessage)]
    if not results:
        return None
    end = read[unit.i1]
    start = read[unit.i0] if isinstance(messages[unit.i0], AIMessage) else read[unit.i0 - 1]
    if end is None:
        return None
    msg = messages[results[0]]
    assert isinstance(msg, ToolMessage)  # noqa: S101
    preview = _flat(_result_body(msg), PREVIEW_CHARS)
    return DisplayBlock("output", unit.i0, unit.i1, start or end, end, None, preview)


def display_blocks(
    units: Sequence[MessageUnit],
    messages: Sequence[BaseMessage],
    read: Sequence[datetime | None],
) -> list[DisplayBlock]:
    """The timeline's blocks for the units of `divide_units(messages)`, in message order."""
    blocks: list[DisplayBlock] = []
    for unit in units:
        msg = messages[unit.i0]
        if unit.kind in ("inbound", "note"):
            placed = read_unit(unit, read)
            if placed.start is not None and placed.end is not None:
                blocks.append(
                    DisplayBlock(
                        unit.kind,
                        unit.i0,
                        unit.i1,
                        placed.start,
                        placed.end,
                        unit.source,
                        unit.preview,
                    )
                )
        elif isinstance(msg, AIMessage):
            turn = _turn_blocks(unit.i0, msg, read)
            if unit.kind == "text":
                blocks.extend(b for k, b in turn.items() if k == "text")
            else:
                blocks.extend(b for k, b in turn.items() if k != "text")
        if unit.kind == "work" and (output := _output_block(unit, messages, read)) is not None:
            blocks.append(output)
    return blocks
