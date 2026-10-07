"""Level-1 grouping inside a chunk call — the reply envelope and its unit numbers.

A chunk call divides its stretch of layer-0 units (`units.py`) into consecutive groups and
writes one level-1 summary per group. The instruction ends with a numbered catalog of the
stretch's units, one line each (`[number] type: content`), and the reply is plain text:

    <group first="1" last="16">summary of the group of units 1 to 16</group>
    <group first="17" last="30">summary of the group of units 17 to 30</group>

`first` and `last` are the catalog numbers of the group's first and last unit, the same envelope
as the upper levels. Every unit of the stretch is in a group, so the groups tile the catalog:
the first starts at unit 1, each starts right after the previous one ends, and the last ends at
the catalog's last unit. A topic that crosses into the next chunk is two nodes there (the upper
levels join them). Nothing is matched against text: a number is looked up. Writing both ends makes
the number checkable: a model that numbers its groups 1, 2, 3 instead of naming units cannot end
its last group at the catalog's end, so the reply is refused instead of being stored with every
span shifted. Every problem of a reply is collected so one correction round can fix them all:

- a malformed envelope: a `<group` / `<open` tag that does not end up as its own complete
  `<group>...</group>` element (a missing `</group>` swallows the next group into the previous
  summary, an unclosed last group is dropped, an `<open/>` element does not exist), which would
  otherwise merge groups silently;
- a number that is not in the catalog, a `last` before its `first`;
- a first group that does not start at unit 1, a group that does not start right after the
  previous one's `last`, a last group that does not end at the last unit, an empty summary.

A group that starts on a work unit whose turn also has a text unit (the two share one
AIMessage) starts at that text unit instead, deterministically, with no complaint: a group never
begins between a turn's text and its tool calls (which also moves the end of the previous group),
and two groups never store the same span. Group sizes are not constrained: the model decides.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from base.agents.history.hierarchy.group import GroupReplyError
from base.agents.history.hierarchy.units import MessageUnit

_OPENING = re.compile(r"<(?:group|open)\b")
_ITEM = re.compile(r"<group\s+first=\"?(\d+)\"?\s+last=\"?(\d+)\"?\s*>(.*?)</group>", re.DOTALL)
_HINT_CHARS = 40


@dataclass(frozen=True)
class Draft:
    """One group as the reply wrote it: the catalog numbers of its first and last unit and its
    summary."""

    first: int
    last: int
    summary: str


@dataclass(frozen=True)
class UnitGroup:
    """One group resolved to units: indices (0-based) into the stretch's unit list, inclusive."""

    first: int
    last: int
    summary: str


def parse_reply(text: str) -> list[Draft]:
    """The groups of a reply, in order.

    Raises:
        GroupReplyError: no `<group>` element, or a `<group` / `<open` tag that is not a
            complete element.
    """
    opened = len(_OPENING.findall(text))
    drafts = [
        Draft(int(first), int(last), summary.strip())
        for first, last, summary in _ITEM.findall(text)
    ]
    if opened != len(drafts):
        raise GroupReplyError(
            f"the reply opens {opened} <group> / <open> tags but holds {len(drafts)} complete "
            'groups: write every group as <group first="N" last="M">summary</group>, close it '
            "with </group>, and use no other tag (there is no <open> element; no summary "
            "contains either tag)"
        )
    if not drafts:
        raise GroupReplyError('the reply has no <group first="N" last="M">summary</group> element')
    return drafts


def _line(catalog: Sequence[str], number: int) -> str:
    """The start of the catalog line of unit `number` (it opens with `[number] `), for a hint."""
    return catalog[number - 1][:_HINT_CHARS] if 1 <= number <= len(catalog) else f"[{number}]"


def _group_problems(
    n: int, draft: Draft, previous: Draft | None, count: int, *, is_last: bool
) -> list[str]:
    """What is wrong with group `n` (0-based) of the reply, in the order the checks run."""
    problems: list[str] = []
    for name, value in (("first", draft.first), ("last", draft.last)):
        if not 1 <= value <= count:
            problems.append(f"{name} {value} is not in the catalog (units 1 to {count})")
    if problems:
        return problems
    if draft.last < draft.first:
        problems.append(f"last {draft.last} is before first {draft.first}")
    if previous is None:
        if draft.first != 1:
            problems.append("the first group starts at unit 1")
    elif draft.first != previous.last + 1:
        problems.append(
            f"group {n + 1} starts at {draft.first}, but the previous group ends at "
            f"{previous.last}: each group starts at the unit right after the previous one's last"
        )
    if is_last and draft.last != count:
        problems.append(
            f"the last group ends at {draft.last}, but the catalog ends at unit {count}: "
            "every unit is in a group"
        )
    if not draft.summary:
        problems.append(f"group {n + 1} has an empty summary")
    return problems


def _start_unit(
    n: int, draft: Draft, units: Sequence[MessageUnit], starts: Sequence[int]
) -> tuple[int | None, str | None]:
    """The 0-based unit group `n` starts at (a start on the tool calls of a turn whose text comes
    right before moves onto that text), or None with the problem when it lands on or before the
    previous group's start."""
    index = draft.first - 1
    if n and units[index].i0 == units[index - 1].i1:
        index -= 1
    if starts and index <= starts[-1]:
        return None, (
            f"group {n + 1} starts on the tool calls of unit {index + 1}'s turn, which moves "
            "it onto the text before them and onto the previous group's start; a group "
            "never begins between a turn's text and its tool calls"
        )
    return index, None


def resolve_groups(
    drafts: Sequence[Draft], units: Sequence[MessageUnit], catalog: Sequence[str] = ()
) -> list[UnitGroup]:
    """The drafts as groups over `units`, tiling the stretch.

    `catalog` is the catalog's lines (index 0 = unit 1); a refusal quotes the start of the lines of
    the offending group's `first` and `last` so the model can check its numbers against them.

    Raises:
        GroupReplyError: any problem listed in the module docstring; the message names them all.
    """
    count = len(units)
    problems: list[str] = []
    starts: list[int] = []
    for n, draft in enumerate(drafts):
        previous = drafts[n - 1] if n else None
        found = _group_problems(n, draft, previous, count, is_last=n == len(drafts) - 1)
        if found:
            if catalog and 1 <= draft.first <= count and 1 <= draft.last <= count:
                ends = f"{_line(catalog, draft.first)} .. {_line(catalog, draft.last)}"
                found[0] += f" (your group: {ends})"
            problems.extend(found)
            continue
        index, problem = _start_unit(n, draft, units, starts)
        if index is None and problem is not None:
            problems.append(problem)
        elif index is not None:
            starts.append(index)
    if problems:
        problems.append(
            "first and last are unit numbers from the catalog, not the position of the group in "
            "the reply: the first group starts at unit 1 and the last group ends at the last "
            "unit of the catalog"
        )
        raise GroupReplyError("\n".join(f"- {p}" for p in problems))
    ends_at = [*(s - 1 for s in starts[1:]), count - 1]
    return [UnitGroup(s, e, d.summary) for s, e, d in zip(starts, ends_at, drafts, strict=True)]
