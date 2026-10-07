"""The chunk call's group envelope: parsing, unit numbers resolved to units, the tiling rules."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from base.agents.history.hierarchy.group import GroupReplyError
from base.agents.history.hierarchy.leaf_groups import (
    Draft,
    UnitGroup,
    parse_reply,
    resolve_groups,
)
from base.agents.history.hierarchy.units import MessageUnit, divide_units


def ts(second: int) -> dict[str, str]:
    return {"ava_created_at": f"2026-10-04T12:00:{second:02d}+00:00"}


def ai(second: int, *, text: str | None = None, code: str | None = None) -> AIMessage:
    blocks: list[str | dict[str, str]] = [{"type": "thinking", "thinking": "hm"}]
    if text is not None:
        blocks.append({"type": "text", "text": text})
    calls = [{"name": "execute_code", "args": {"code": code}, "id": f"tc{second}"}] if code else []
    return AIMessage(content=blocks, tool_calls=calls, additional_kwargs=ts(second))


def result(second: int) -> ToolMessage:
    return ToolMessage(
        content="ok",
        tool_call_id="tc",
        additional_kwargs={"ava_msg_type": "exec_output", **ts(second)},
    )


def inbound(second: int, text: str) -> HumanMessage:
    return HumanMessage(
        content=text,
        additional_kwargs={"ava_msg_type": "inbound", "ava_source": "user", **ts(second)},
    )


# unit 1 inbound, 2 text, 3 work (shares message 1 with the text), 4 work, 5 inbound, 6 work
MESSAGES: list[BaseMessage] = [
    inbound(1, "fix the flaky test"),
    ai(2, text="I will look at it", code="ls -la"),
    result(3),
    ai(4, code="pytest -x tests/a.py"),
    result(5),
    inbound(6, "thanks, now deploy"),
    ai(7, code="pytest -x tests/b.py"),
    result(8),
]
UNITS = divide_units(MESSAGES)


CATALOG = [f"line of unit {n}" for n in range(1, 7)]


def resolve(reply: str) -> list[UnitGroup]:
    return resolve_groups(parse_reply(reply), UNITS, CATALOG)


def group(summary: str, first: int, last: int) -> str:
    return f'<group first="{first}" last="{last}">{summary}</group>'


def test_the_units_the_numbers_refer_to() -> None:
    assert [(u.kind, u.i0, u.i1) for u in UNITS] == [
        ("inbound", 0, 0),
        ("text", 1, 1),
        ("work", 1, 2),
        ("work", 3, 4),
        ("inbound", 5, 5),
        ("work", 6, 7),
    ]


def test_numbers_resolve_to_unit_boundaries_and_groups_tile_the_stretch() -> None:
    reply = "\n".join(
        [group("investigation", 1, 3), group("second part", 4, 4), group("deploy", 5, 6)]
    )
    assert resolve(reply) == [
        UnitGroup(0, 2, "investigation"),
        UnitGroup(3, 3, "second part"),
        UnitGroup(4, 5, "deploy"),
    ]


def test_one_group_may_cover_the_whole_stretch_and_other_text_is_ignored() -> None:
    reply = "thinking aloud\n" + group("all of it", 1, 6) + "\ntrailing"
    assert resolve(reply) == [UnitGroup(0, 5, "all of it")]


def test_the_attributes_may_be_unquoted_or_spaced() -> None:
    drafts = parse_reply('<group first=1 last=3>a</group><group  first="4"  last="6" >b</group>')
    assert drafts == [Draft(1, 3, "a"), Draft(4, 6, "b")]


def test_a_number_outside_the_catalog_is_refused() -> None:
    for number in (0, 7, 99):
        with pytest.raises(GroupReplyError, match="is not in the catalog \\(units 1 to 6\\)"):
            resolve(group("a", 1, 3) + group("b", 4, number))
        with pytest.raises(GroupReplyError, match=f"first {number} is not in the catalog"):
            resolve(group("a", number, 3) + group("b", 4, 6))


def test_a_group_that_ends_before_it_starts_is_refused() -> None:
    with pytest.raises(GroupReplyError, match="last 2 is before first 4"):
        resolve(group("a", 1, 3) + group("b", 4, 2) + group("c", 3, 6))


def test_groups_that_leave_a_gap_or_overlap_are_refused() -> None:
    with pytest.raises(
        GroupReplyError, match="group 2 starts at 5, but the previous group ends at 3"
    ):
        resolve(group("a", 1, 3) + group("b", 5, 6))
    with pytest.raises(
        GroupReplyError, match="group 2 starts at 3, but the previous group ends at 4"
    ):
        resolve(group("a", 1, 4) + group("b", 3, 6))


def test_the_first_group_starts_at_one_and_the_last_ends_at_the_catalogs_end() -> None:
    with pytest.raises(GroupReplyError, match="the first group starts at unit 1"):
        resolve(group("a", 2, 3) + group("b", 4, 6))
    with pytest.raises(
        GroupReplyError, match="the last group ends at 5, but the catalog ends at unit 6"
    ):
        resolve(group("a", 1, 3) + group("b", 4, 5))


def test_numbering_the_groups_instead_of_the_units_is_refused() -> None:
    """The regression of job 149: n = 61 units, a reply that counts its ten groups 1..10 as
    `first` (and, for the model's own idea of a span, last = first). Before the envelope carried
    both ends, those numbers passed every check and every node's span was shifted."""
    count = 61
    units = [MessageUnit("text", i, i, None, None, None, f"u{i}") for i in range(count)]
    catalog = [f"[{n}] text: unit {n}" for n in range(1, count + 1)]
    reply = "".join(group(f"g{k}", k, k) for k in range(1, 11))
    with pytest.raises(GroupReplyError) as exc:
        resolve_groups(parse_reply(reply), units, catalog)
    text = str(exc.value)
    assert "the last group ends at 10, but the catalog ends at unit 61" in text
    assert "not the position of the group in the reply" in text
    assert "(your group: [10] text: unit 10 .. [10] text: unit 10)" in text
    # The same numbers as a faithful reply would write them are accepted.
    firsts = [1, 7, 13, 19, 25, 31, 37, 43, 49, 55]
    ok = "".join(
        group(f"g{k}", f, (firsts[k + 1] - 1) if k + 1 < len(firsts) else count)
        for k, f in enumerate(firsts)
    )
    groups = resolve_groups(parse_reply(ok), units, catalog)
    assert (groups[0].first, groups[0].last, groups[-1].first, groups[-1].last) == (0, 5, 54, 60)


def test_a_start_on_a_turns_tool_calls_moves_to_its_text_without_complaint() -> None:
    # unit 3 is the work unit of the message whose text is unit 2: the boundary moves one earlier
    assert resolve(group("a", 1, 2) + group("b", 3, 6)) == [
        UnitGroup(0, 0, "a"),
        UnitGroup(1, 5, "b"),
    ]


def test_a_group_ending_on_a_turns_text_ends_before_it_the_same_way() -> None:
    assert resolve(group("a", 1, 2) + group("b", 3, 6))[0].last == 0


def test_the_move_can_make_a_start_repeat_and_that_is_refused() -> None:
    with pytest.raises(GroupReplyError, match="never begins between a turn's text"):
        resolve(group("a", 1, 1) + group("b", 2, 2) + group("c", 3, 6))


def test_every_problem_of_a_reply_is_reported_at_once() -> None:
    reply = group("a", 2, 3) + group("", 5, 5) + group("c", 6, 99)
    with pytest.raises(GroupReplyError) as exc:
        resolve(reply)
    lines = str(exc.value).splitlines()
    assert all(line.startswith("- ") for line in lines)
    joined = "\n".join(lines)
    assert "the first group starts at unit 1" in joined
    assert "group 2 starts at 5, but the previous group ends at 3" in joined
    assert "empty summary" in joined and "last 99 is not in the catalog" in joined


def test_group_sizes_are_not_constrained() -> None:
    assert resolve(group("a", 1, 5) + group("b", 6, 6)) == [
        UnitGroup(0, 4, "a"),
        UnitGroup(5, 5, "b"),
    ]


def test_there_is_no_open_element_a_reply_using_one_is_refused() -> None:
    for reply in (group("a", 1, 3) + '<open start="5"/>', '<open start="1"/>', "<open/>"):
        with pytest.raises(GroupReplyError, match=r"there is no <open> element"):
            parse_reply(reply)


@pytest.mark.parametrize(
    "reply",
    [
        "no groups at all",
        "<groups></groups>",
        '<group first="3" last="4"></group',
        "<open start='x'/>",
        '<group start="1">no first or last</group>',
    ],
)
def test_a_reply_without_a_group_is_refused(reply: str) -> None:
    with pytest.raises(GroupReplyError, match=r"no <group|opens \d+ <group>"):
        parse_reply(reply)


def test_summaries_keep_their_text_including_other_markup() -> None:
    drafts = parse_reply(group("a <b>bold</b> and\n<summary> mentioned  ", 1, 6))
    assert drafts[0].summary == "a <b>bold</b> and\n<summary> mentioned"


def test_a_missing_close_tag_is_refused_not_merged_into_one_group() -> None:
    # The model left out </group> on the first two groups: the lazy match would take everything
    # up to the last close tag as one summary.
    reply = (
        '<group first="1" last="2">a<group first="3" last="4">b<group first="5" last="6">c</group>'
    )
    with pytest.raises(GroupReplyError, match=r"opens 3 <group> / <open> tags but holds 1"):
        parse_reply(reply)


def test_an_unclosed_last_group_is_refused_not_dropped() -> None:
    with pytest.raises(GroupReplyError, match=r"opens 2 .* holds 1"):
        parse_reply(group("a", 1, 3) + '<group first="4" last="6">b')


def test_a_summary_that_contains_a_group_tag_is_refused() -> None:
    with pytest.raises(GroupReplyError, match="opens 2"):
        parse_reply(
            '<group first="1" last="6">text with a literal <group first="3" last="4"> tag</group>'
        )
