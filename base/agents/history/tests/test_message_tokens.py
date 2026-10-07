"""Per-message true tokens: exact / split / estimated and every fallback edge."""

from __future__ import annotations

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from base.agents.history.checkpoint import FullHistory, single_segment_history
from base.agents.history.message_tokens import (
    MessageTokens,
    PartTokens,
    ai_message_parts,
    history_message_tokens,
    history_segment_tokens,
    segment_tokens,
    split_parts,
    summarize_segments,
    total_of,
)
from base.agents.messages.text_chars import estimate_message_tokens


def _est(msg: BaseMessage) -> int:
    return round(estimate_message_tokens(msg))


def _ai(text: str, *, inp: int | None, out: int | None, model: str | None = None) -> AIMessage:
    msg = AIMessage(content=text)
    if inp is not None or out is not None:
        msg.usage_metadata = {  # pyright: ignore[reportAttributeAccessIssue]
            "input_tokens": inp or 0,
            "output_tokens": out or 0,
            "total_tokens": (inp or 0) + (out or 0),
        }
    if model is not None:
        msg.response_metadata = {"model_name": model}
    return msg


def _human(chars: int) -> HumanMessage:
    return HumanMessage(content="h" * chars)


def _tool(chars: int, call_id: str = "t") -> ToolMessage:
    return ToolMessage(content="t" * chars, tool_call_id=call_id)


SYS = SystemMessage(content="s" * 400)  # estimate 100


def test_head_split_by_estimate_when_first_request_known() -> None:
    human = _human(1600)
    seg = segment_tokens(SYS, [human, _ai("a", inp=1000, out=10)])
    assert seg.head is not None
    assert seg.head.source == seg.messages[0].source == "split"
    assert seg.head.context_tokens + seg.messages[0].context_tokens == 1000
    # Shares follow the estimator's weights (within rounding).
    want = 1000 * _est(SYS) / (_est(SYS) + _est(human))
    assert abs(seg.head.context_tokens - want) <= 1
    assert seg.messages[1] == MessageTokens(10, 10, "exact")


def test_lone_head_takes_first_request_input_exactly() -> None:
    seg = segment_tokens(SYS, [_ai("a", inp=777, out=5)])
    assert seg.head == MessageTokens(777, None, "exact")


def test_single_message_interval_is_exact() -> None:
    body = [_ai("a", inp=1000, out=50), _tool(40), _ai("b", inp=1300, out=20)]
    seg = segment_tokens(None, body)
    assert seg.messages[1] == MessageTokens(250, None, "exact")  # 1300-1000-50
    assert seg.head is None


def test_multi_message_interval_split_by_estimate_and_conserves_total() -> None:
    big, small = _tool(300, "1"), _tool(100, "2")
    body = [_ai("a", inp=1000, out=50), big, small, _ai("b", inp=1450, out=20)]
    first, second = segment_tokens(None, body).messages[1:3]
    assert (first.source, second.source) == ("split", "split")
    assert first.context_tokens + second.context_tokens == 1450 - 1000 - 50
    want = 400 * _est(big) / (_est(big) + _est(small))
    assert abs(first.context_tokens - want) <= 1


def test_split_rounding_sums_exactly() -> None:
    body = [
        _ai("a", inp=0 + 10, out=1),
        _tool(4, "1"),
        _tool(4, "2"),
        _tool(4, "3"),
        _ai("b", inp=20, out=1),
    ]
    got = [m.context_tokens for m in segment_tokens(None, body).messages[1:4]]
    assert sum(got) == 20 - 10 - 1
    assert max(got) - min(got) <= 1


def test_zero_estimate_interval_splits_evenly() -> None:
    body = [_ai("a", inp=10, out=2), _tool(0, "1"), _tool(0, "2"), _ai("b", inp=30, out=2)]
    got = [m.context_tokens for m in segment_tokens(None, body).messages[1:3]]
    assert got == [9, 9]


def test_negative_difference_falls_back_to_estimate() -> None:
    body = [_ai("a", inp=5000, out=50), _tool(400), _ai("b", inp=1000, out=20)]
    seg = segment_tokens(None, body)
    assert seg.messages[1] == MessageTokens(_est(body[1]), None, "estimated")
    assert seg.messages[0].source == "exact"  # the AI keeps its own output_tokens


def test_difference_smaller_than_previous_output_is_negative() -> None:
    body = [_ai("a", inp=1000, out=500), _tool(400), _ai("b", inp=1200, out=20)]
    assert segment_tokens(None, body).messages[1].source == "estimated"


def test_model_switch_falls_back_to_estimate() -> None:
    body = [
        _ai("a", inp=1000, out=50, model="m1"),
        _tool(400),
        _ai("b", inp=1300, out=20, model="m2"),
    ]
    assert segment_tokens(None, body).messages[1].source == "estimated"


def test_same_model_keeps_exact() -> None:
    body = [
        _ai("a", inp=1000, out=50, model="m1"),
        _tool(400),
        _ai("b", inp=1300, out=20, model="m1"),
    ]
    assert segment_tokens(None, body).messages[1].source == "exact"


def test_ai_without_usage_is_estimated_and_breaks_both_adjacent_intervals() -> None:
    body = [
        _ai("a", inp=1000, out=50),
        _tool(400, "1"),
        _ai("x" * 80, inp=None, out=None),
        _tool(400, "2"),
        _ai("c", inp=2000, out=20),
    ]
    seg = segment_tokens(None, body)
    assert seg.messages[2] == MessageTokens(_est(body[2]), _est(body[2]), "estimated")
    assert seg.messages[1].source == "estimated"
    assert seg.messages[3].source == "estimated"


def test_first_request_without_usage_estimates_the_head() -> None:
    seg = segment_tokens(SYS, [_human(400), _ai("a", inp=None, out=None)])
    assert seg.head == MessageTokens(_est(SYS), None, "estimated")
    assert seg.messages[0] == MessageTokens(_est(_human(400)), None, "estimated")


def test_tail_after_last_request_is_estimated() -> None:
    body = [
        _ai("a", inp=1000, out=50),
        _tool(400),
        _ai("b", inp=1300, out=20),
        _tool(800),
        _human(40),
    ]
    seg = segment_tokens(None, body)
    assert seg.messages[3] == MessageTokens(_est(body[3]), None, "estimated")
    assert seg.messages[4] == MessageTokens(_est(body[4]), None, "estimated")
    assert seg.last_input_tokens == 1300


def test_segment_without_any_ai_is_all_estimated() -> None:
    seg = segment_tokens(SYS, [_human(40)])
    assert {seg.head.source, seg.messages[0].source} == {"estimated"}  # pyright: ignore[reportOptionalMemberAccess]
    assert seg.last_input_tokens is None


def test_empty_segment() -> None:
    seg = segment_tokens(None, [])
    assert seg.head is None and seg.messages == ()


def test_stitched_history_restarts_anchor_at_each_segment() -> None:
    seg0: list[BaseMessage] = [_human(400), _ai("a", inp=500, out=10)]
    seg1: list[BaseMessage] = [
        _human(40),
        _ai("b", inp=900, out=10),
        _tool(8),
        _ai("c", inp=950, out=5),
    ]
    head0, head1 = SystemMessage(content="x" * 400), SystemMessage(content="y" * 400)
    history = FullHistory([*seg0, *seg1], (head0, head1), (0, 2))
    segs = history_segment_tokens(history)
    assert len(segs) == 2
    assert segs[0].head.context_tokens + segs[0].messages[0].context_tokens == 500  # pyright: ignore[reportOptionalMemberAccess]
    assert segs[1].head.context_tokens + segs[1].messages[0].context_tokens == 900  # pyright: ignore[reportOptionalMemberAccess]
    # Segment 1's interval uses only its own requests: 950 - 900 - 10.
    assert segs[1].messages[2] == MessageTokens(40, None, "exact")
    flat = history_message_tokens(history)
    assert len(flat) == len(history.messages)


def test_summaries_per_segment() -> None:
    body: list[BaseMessage] = [
        _human(1600),
        _ai("a", inp=1000, out=50),
        _tool(40),
        _ai("b", inp=1300, out=20),
        _tool(40),
    ]
    history = single_segment_history([SYS, *body])
    (summary,) = summarize_segments(history)
    assert summary.message_count == 5
    assert summary.generation_tokens == 70
    assert summary.last_input_tokens == 1300
    assert summary.context_tokens == sum(summary.tokens_by_source.values())
    # head + user message anchor to the first request; AI outputs are exact.
    assert summary.tokens_by_source["split"] == 1000
    assert summary.messages_by_source == {"exact": 3, "split": 2, "estimated": 1}


def test_empty_history_has_no_segments() -> None:
    assert history_segment_tokens(FullHistory([], (), ())) == ()
    assert history_message_tokens(FullHistory([], (), ())) == []


def test_cjk_costs_more_than_latin_and_whitespace_is_free() -> None:
    assert _est(HumanMessage(content=chr(0x4E2D) * 100)) > _est(HumanMessage(content="a" * 100)) * 2
    assert _est(HumanMessage(content="a b " * 10)) == _est(HumanMessage(content="ab" * 10))


def test_summary_marks_estimated_when_any_part_is_not_exact() -> None:
    exact = MessageTokens(10, None, "exact")
    assert total_of([exact, exact]).estimated is False
    assert total_of([exact, exact]).exact_fraction == 1.0
    mixed = total_of([exact, MessageTokens(30, None, "split")])
    assert mixed.estimated is True
    assert mixed.exact_fraction == 0.25
    assert total_of([MessageTokens(5, None, "estimated")]).estimated is True
    assert total_of([]).estimated is False


def test_segment_summary_carries_the_marker() -> None:
    clean = history_segment_tokens(single_segment_history([_ai("a", inp=500, out=5)]))
    assert (
        summarize_segments(single_segment_history([_ai("a", inp=500, out=5)]))[0].estimated is False
    )
    assert clean[0].messages[0].source == "exact"
    (tail,) = summarize_segments(single_segment_history([_ai("a", inp=500, out=5), _tool(40)]))
    assert tail.estimated is True
    assert 0 < tail.exact_fraction < 1


def test_ai_message_parts_are_split_and_conserve_the_whole() -> None:
    msg = AIMessage(
        content=[
            {"type": "thinking", "thinking": chr(0x601D) * 90},
            {"type": "text", "text": "x" * 30},
        ],
        tool_calls=[{"name": "execute_code", "args": {"code": "y" * 60}, "id": "c1"}],
    )
    parts = ai_message_parts(msg, MessageTokens(200, 200, "exact"))
    assert sum(p.tokens for p in parts.values()) == 200
    assert {p.source for p in parts.values()} == {"split"}
    assert parts["reasoning"].tokens > parts["output"].tokens
    est = ai_message_parts(msg, MessageTokens(200, 200, "estimated"))
    assert {p.source for p in est.values()} == {"estimated"}


def test_split_parts_lone_part_keeps_source_and_sections_conserve() -> None:
    assert split_parts({"only": "abc"}, 7, "exact") == {"only": PartTokens(7, "exact")}
    sections = split_parts({"a": "x" * 10, "b": "y" * 33, "c": ""}, 101, "exact")
    assert sum(p.tokens for p in sections.values()) == 101
    assert sections["c"].tokens == 0
