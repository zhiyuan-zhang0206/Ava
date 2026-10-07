# pyright: reportUnknownMemberType = warning
# pyright: reportUnknownArgumentType = warning
# pyright: reportUnknownLambdaType = warning
# pyright: reportUnknownVariableType = warning
"""Upper-level grouping: the plain-text reply rules and the correction conversation."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from base.agents.history.hierarchy.generate import GenerateError
from base.agents.history.hierarchy.group import (
    Group,
    GroupCall,
    GroupReplyError,
    OpenNode,
    build_group_prompt,
    generate_groups,
    parse_groups,
)
from base.clock import Clock
from base.config import settings


@pytest.fixture(autouse=True)
def _cluster_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.general, "timezone", "Asia/Shanghai")
    monkeypatch.setattr(settings.general, "message_timestamp_weekday", False)


def _nodes(count: int) -> list[OpenNode]:
    """Open nodes with ids 100, 101, ...."""
    return [
        OpenNode(
            id=100 + i,
            span_start=i * 10,
            span_end=i * 10 + 9,
            start=datetime(2026, 10, 5, i, 0, tzinfo=UTC),
            end=datetime(2026, 10, 5, i, 30, tzinfo=UTC),
            text=f"node {i}",
        )
        for i in range(count)
    ]


def _reply(*groups: tuple[int, int]) -> str:
    return "\n".join(f'<group first="{a}" last="{b}">s{a}</group>' for a, b in groups)


def test_valid_reply_yields_groups_and_leaves_the_tail_open() -> None:
    assert parse_groups(_reply((100, 102)), _nodes(5), must_close=False) == [
        Group(100, 102, "s100")
    ]
    assert parse_groups(_reply((100, 102), (103, 105)), _nodes(8), must_close=False) == [
        Group(100, 102, "s100"),
        Group(103, 105, "s103"),
    ]


def test_the_reply_may_carry_other_text_and_loose_attribute_quoting() -> None:
    text = 'thinking aloud <group first=100 last="101" >  a\nb </group> trailing'
    assert parse_groups(text, _nodes(5), must_close=False) == [Group(100, 101, "a\nb")]


def test_a_reply_without_groups_declines() -> None:
    assert parse_groups("", _nodes(5), must_close=False) == []
    assert parse_groups("nothing is ready to close yet", _nodes(5), must_close=False) == []


@pytest.mark.parametrize(
    ("reply", "problem"),
    [
        (_reply((100, 999)), "last=999"),
        (_reply((999, 100)), "first=999"),
        (_reply((101, 103)), "should start at id 100"),
        (_reply((100, 102), (104, 106)), "should start at id 103"),
        (_reply((100, 102), (103, 101)), "ends before it starts"),
        (_reply((100, 102), (103, 106)), "newest summary"),
        ('<group first="100" last="102"> </group>', "empty"),
    ],
)
def test_reply_rules(reply: str, problem: str) -> None:
    with pytest.raises(GroupReplyError, match=problem):
        parse_groups(reply, _nodes(7), must_close=False)


def test_group_sizes_above_one_are_not_constrained() -> None:
    nodes = _nodes(23)
    assert parse_groups(_reply((100, 101)), nodes, must_close=False)[0].last == 101  # two nodes
    assert parse_groups(_reply((100, 120)), nodes, must_close=False)[0].last == 120  # 21 nodes


def test_an_empty_reply_is_refused_only_when_the_open_set_must_shrink() -> None:
    assert parse_groups("", _nodes(23), must_close=False) == []
    with pytest.raises(GroupReplyError, match="at least one group must be closed"):
        parse_groups("", _nodes(23), must_close=True)


def test_prompt_lists_nodes_by_id_with_cluster_time_and_states_purpose_rules_and_format() -> None:
    text = build_group_prompt(_nodes(5), clock=Clock.from_settings(), must_close=False)
    assert "[id 100] [2026-10-05 08:00:00] to [2026-10-05 08:30:00]\nnode 0" in text
    assert "its reader can always open the members below it" in text
    assert "a group may span a wider matter than the matter of any one summary" in text
    assert "shorter than the summaries it covers put together" in text
    assert "much shorter" not in text
    assert "about the same matter" in text and "faithfully" not in text
    assert "follow one another without gaps" in text
    assert (
        "Leave the newest summary out of every group; you may also leave out the last few" in text
    )
    assert "stays open" not in text and "No groups at all is a fine answer." in text
    assert '<group first="ID" last="ID">summary</group>' in text and "Do not call any tool" in text
    assert "per group" not in text and "to 15" not in text  # no size rule
    assert "must be closed" not in text
    assert "must be closed" in build_group_prompt(
        _nodes(5), clock=Clock.from_settings(), must_close=True
    )


_USAGE: Any = {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}


class _Llm:
    """A chat-model stand-in: plain `invoke`, replies in order (a string, or an exception)."""

    def __init__(self, *replies: str | Exception) -> None:
        self.replies = list(replies)
        self.seen: list[list[Any]] = []

    def invoke(self, messages: list[Any]) -> AIMessage:
        self.seen.append(list(messages))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return AIMessage(content=reply, usage_metadata=_USAGE)


def _run(
    llm: _Llm, nodes: list[OpenNode], *, corrections: int = 2
) -> tuple[list[Group], list[GroupCall]]:
    calls: list[GroupCall] = []
    groups = generate_groups(
        llm,
        nodes,
        model="m",
        corrections=corrections,
        clock=Clock.from_settings(),
        retry_attempts=0,
        on_call=calls.append,
    )
    return groups, calls


def test_a_valid_reply_is_one_call_without_tools() -> None:
    llm = _Llm(_reply((100, 102)))
    groups, calls = _run(llm, _nodes(5))
    assert groups == [Group(100, 102, "s100")]
    assert [(c.round, c.problem) for c in calls] == [(0, None)]
    assert len(llm.seen) == 1 and "[id 104]" in llm.seen[0][0].content


def test_a_refused_reply_is_corrected_in_the_same_conversation() -> None:
    llm = _Llm(_reply((101, 102)), _reply((100, 102)))
    groups, calls = _run(llm, _nodes(5))
    assert groups == [Group(100, 102, "s100")]
    assert [(c.round, c.problem is None) for c in calls] == [(0, False), (1, True)]
    assert "should start at id 100" in (calls[0].problem or "")
    second = llm.seen[1]  # prompt, the model's reply, the correction
    assert [type(m).__name__ for m in second] == ["HumanMessage", "AIMessage", "HumanMessage"]
    assert (
        "should start at id 100" in second[2].content
        and "Do not call any tool" in second[2].content
    )
    assert calls[1].request.startswith("Your reply cannot be used")


def test_corrections_run_out_into_a_failure_with_every_call_recorded() -> None:
    llm = _Llm(_reply((101, 102)), _reply((101, 102)), _reply((101, 102)))
    calls: list[GroupCall] = []
    with pytest.raises(GenerateError, match="after 2 correction"):
        generate_groups(
            llm,
            _nodes(5),
            model="m",
            corrections=2,
            clock=Clock.from_settings(),
            retry_attempts=0,
            on_call=calls.append,
        )
    assert len(calls) == 3 and all(c.problem for c in calls)


def test_zero_corrections_fail_on_the_first_refusal() -> None:
    with pytest.raises(GenerateError):
        _run(_Llm(_reply((101, 102))), _nodes(5), corrections=0)


def test_must_close_is_passed_to_the_prompt_and_the_check() -> None:
    llm = _Llm("", _reply((100, 102)))
    calls: list[GroupCall] = []
    groups = generate_groups(
        llm,
        _nodes(5),
        model="m",
        corrections=2,
        clock=Clock.from_settings(),
        must_close=True,
        retry_attempts=0,
        on_call=calls.append,
    )
    assert groups == [Group(100, 102, "s100")]
    assert "must be closed" in llm.seen[0][0].content
    assert "at least one group must be closed" in (calls[0].problem or "")


def test_a_provider_error_is_recorded_and_raised() -> None:
    llm = _Llm(RuntimeError("boom"))
    calls: list[GroupCall] = []
    with pytest.raises(GenerateError):
        generate_groups(
            llm,
            _nodes(5),
            model="m",
            corrections=2,
            clock=Clock.from_settings(),
            retry_attempts=0,
            on_call=calls.append,
        )
    assert len(calls) == 1 and calls[0].response is None and "boom" in (calls[0].error or "")


def test_an_unclosed_group_tag_is_refused_not_merged() -> None:
    reply = '<group first="100" last="101">a <group first="102" last="103">b</group>'
    with pytest.raises(GroupReplyError, match="opens 2 <group> tags but holds 1"):
        parse_groups(reply, _nodes(6), must_close=False)
    with pytest.raises(GroupReplyError, match="opens 2 <group> tags but holds 1"):
        parse_groups(
            '<group first="100" last="101">a</group><group first="102" last="103">b',
            _nodes(6),
            must_close=False,
        )


def test_a_refused_malformed_reply_is_corrected_in_the_same_conversation() -> None:
    llm = _Llm(
        '<group first="100" last="101">a <group first="102" last="103">b</group>',
        _reply((100, 102)),
    )
    groups, calls = _run(llm, _nodes(6))
    assert groups == [Group(100, 102, "s100")]
    assert "opens 2 <group> tags" in (calls[0].problem or "") and calls[1].problem is None


def test_the_understanding_calls_retry_a_rate_limit_five_times() -> None:
    import inspect

    from base.agents.history.hierarchy.chunk_generate import generate_chunk
    from base.agents.history.hierarchy.group import generate_groups

    for fn in (generate_groups, generate_chunk):
        assert inspect.signature(fn).parameters["retry_attempts"].default == 5


def test_only_a_group_of_one_summary_at_the_end_stays_open_without_an_error() -> None:
    reply = _reply((100, 102), (103, 103), (104, 106), (107, 107))
    assert parse_groups(reply, _nodes(9), must_close=False) == [
        Group(100, 102, "s100"),
        Group(103, 103, "s103"),
        Group(104, 106, "s104"),
    ]
    assert parse_groups(_reply((100, 100)), _nodes(5), must_close=False) == []


def test_only_single_groups_under_must_close_close_nothing_and_are_refused() -> None:
    with pytest.raises(GroupReplyError, match="at least one group must be closed"):
        parse_groups(_reply((100, 100), (101, 101)), _nodes(5), must_close=True)


def test_a_huge_number_and_a_mention_of_the_tag_are_handled_without_an_exception() -> None:
    nodes = _nodes(8)
    with pytest.raises(GroupReplyError, match="opens 1 <group> tags but holds 0"):
        parse_groups(
            '<group first="100" last="' + "9" * 5000 + '">a</group>', nodes, must_close=False
        )
    text = '<group first="100" last="102">on the <group> tag and <group first x</group>'
    assert parse_groups(text, nodes, must_close=False) == [
        Group(100, 102, "on the <group> tag and <group first x")
    ]


def test_a_trailing_single_group_stays_open_and_the_others_close_in_order() -> None:
    nodes = _nodes(8)
    middle = _reply((100, 100), (101, 103), (104, 104), (105, 106))
    assert [(g.first, g.last) for g in parse_groups(middle, nodes, must_close=False)] == [
        (100, 100),
        (101, 103),
        (104, 104),
        (105, 106),
    ]
    tail = _reply((100, 102), (103, 103))
    assert [(g.first, g.last) for g in parse_groups(tail, nodes, must_close=False)] == [(100, 102)]
