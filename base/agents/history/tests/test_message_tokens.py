"""Per-message true tokens: exact / split / estimated and every fallback edge."""

from __future__ import annotations

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from base.agents.history.checkpoint import FullHistory, single_segment_history
from base.agents.history.message_tokens import (
    MessageTokens,
    history_message_tokens,
    history_segment_tokens,
    segment_tokens,
    summarize_segments,
)


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
    body: list[BaseMessage] = [_human(1600), _ai("a", inp=1000, out=10)]
    seg = segment_tokens(SYS, body)
    assert seg.head == MessageTokens(200, None, "split")
    assert seg.messages[0] == MessageTokens(800, None, "split")
    assert seg.head is not None
    assert seg.head.context_tokens + seg.messages[0].context_tokens == 1000
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
    body = [
        _ai("a", inp=1000, out=50),
        _tool(300, "1"),  # 75
        _tool(100, "2"),  # 25
        _ai("b", inp=1450, out=20),
    ]
    seg = segment_tokens(None, body)
    first, second = seg.messages[1], seg.messages[2]
    assert (first.source, second.source) == ("split", "split")
    assert (first.context_tokens, second.context_tokens) == (300, 100)  # 400 remainder 3:1
    assert first.context_tokens + second.context_tokens == 1450 - 1000 - 50


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
    assert seg.messages[1] == MessageTokens(100, None, "estimated")
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
    assert seg.messages[2] == MessageTokens(20, 20, "estimated")
    assert seg.messages[1].source == "estimated"
    assert seg.messages[3].source == "estimated"


def test_first_request_without_usage_estimates_the_head() -> None:
    seg = segment_tokens(SYS, [_human(400), _ai("a", inp=None, out=None)])
    assert seg.head == MessageTokens(100, None, "estimated")
    assert seg.messages[0] == MessageTokens(100, None, "estimated")


def test_tail_after_last_request_is_estimated() -> None:
    body = [
        _ai("a", inp=1000, out=50),
        _tool(400),
        _ai("b", inp=1300, out=20),
        _tool(800),
        _human(40),
    ]
    seg = segment_tokens(None, body)
    assert seg.messages[3] == MessageTokens(200, None, "estimated")
    assert seg.messages[4] == MessageTokens(10, None, "estimated")
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
    assert summary.head_tokens == 200
    assert summary.context_tokens == sum(summary.tokens_by_source.values())
    # head + user message anchor to the first request; AI outputs are exact.
    assert summary.tokens_by_source["split"] == 1000
    assert summary.messages_by_source == {"exact": 3, "split": 2, "estimated": 1}


def test_empty_history_has_no_segments() -> None:
    assert history_segment_tokens(FullHistory([], (), ())) == ()
    assert history_message_tokens(FullHistory([], (), ())) == []
