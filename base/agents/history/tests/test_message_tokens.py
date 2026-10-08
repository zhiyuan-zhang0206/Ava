"""Per-message true tokens: exact / split / estimated and every fallback edge."""

from __future__ import annotations

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from base.agents.history.checkpoint import FullHistory, single_segment_history
from base.agents.history.closing_request import ClosingRequest
from base.agents.history.message_tokens import (
    MessageTokens,
    ModelSwitch,
    PartTokens,
    ai_message_parts,
    context_through,
    history_message_tokens,
    history_segment_tokens,
    segment_tokens,
    split_parts,
    summarize_segments,
    total_of,
)
from base.agents.messages.token_estimate import estimate_message_tokens


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


SYS = SystemMessage(content="s" * 400)


def _ctx(seg_msgs: tuple[MessageTokens, ...], lo: int, hi: int) -> int:
    return sum(m.context_tokens or 0 for m in seg_msgs[lo:hi])


def test_head_is_anchored_by_first_request_and_shared_by_estimate() -> None:
    human = _human(1600)
    seg = segment_tokens(SYS, [human, _ai("a", inp=1000, out=10)])
    assert seg.head is not None and seg.head.context_tokens is not None
    assert seg.head.source == seg.messages[0].source == "estimated"
    assert seg.head.context_tokens + (seg.messages[0].context_tokens or 0) == 1000
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


def test_multi_message_interval_is_estimated_and_conserves_total() -> None:
    big, small = _tool(300, "1"), _tool(100, "2")
    body = [_ai("a", inp=1000, out=50), big, small, _ai("b", inp=1450, out=20)]
    first, second = segment_tokens(None, body).messages[1:3]
    assert (first.source, second.source) == ("estimated", "estimated")
    assert (first.context_tokens or 0) + (second.context_tokens or 0) == 1450 - 1000 - 50
    want = 400 * _est(big) / (_est(big) + _est(small))
    assert abs((first.context_tokens or 0) - want) <= 1


def test_apportion_rounding_sums_exactly() -> None:
    body = [
        _ai("a", inp=10, out=1),
        _tool(4, "1"),
        _tool(4, "2"),
        _tool(4, "3"),
        _ai("b", inp=20, out=1),
    ]
    got = [m.context_tokens or 0 for m in segment_tokens(None, body).messages[1:4]]
    assert sum(got) == 20 - 10 - 1
    assert max(got) - min(got) <= 1


def test_ai_generation_is_output_tokens_exact_or_none() -> None:
    body = [_ai("a", inp=1000, out=50), _ai("x", inp=None, out=None), _ai("c", inp=2000, out=7)]
    seg = segment_tokens(None, body)
    assert seg.messages[0] == MessageTokens(50, 50, "exact")
    assert seg.messages[1].generation_tokens is None
    assert seg.messages[2].generation_tokens == 7


def test_negative_difference_drops_the_anchor_and_merges_the_interval() -> None:
    # c contradicts b (1110 < 1100 + 20) but not a, so b is dropped as an anchor.
    body = [
        _ai("a", inp=1000, out=50),
        _tool(40, "1"),
        _ai("b", inp=1100, out=20),
        _tool(40, "2"),
        _ai("c", inp=1110, out=5),
    ]
    seg = segment_tokens(None, body)
    # Interval a -> c merges (t1, b, t2): 1110 - 1000 - 50 shared, estimated.
    assert sum(m.context_tokens or 0 for m in seg.messages[1:4]) == 60
    assert {m.source for m in seg.messages[1:4]} == {"estimated"}
    assert seg.messages[2].generation_tokens == 20  # its own output stays exact
    assert seg.messages[0] == MessageTokens(50, 50, "exact")


def test_negative_everywhere_reanchors_the_whole_context() -> None:
    body = [
        _ai("a", inp=5000, out=50),
        _tool(400, "1"),
        _ai("b", inp=900, out=20),  # history was rewritten: smaller than a's input
        _tool(40, "2"),
        _ai("c", inp=1000, out=10),
    ]
    seg = segment_tokens(None, body)
    assert sum(m.context_tokens or 0 for m in seg.messages[0:2]) == 900
    assert seg.messages[0].source == seg.messages[1].source == "estimated"
    assert seg.messages[0].generation_tokens == 50
    assert seg.messages[3] == MessageTokens(1000 - 900 - 20, None, "exact")


def test_model_switch_keeps_the_values_messages_were_read_with() -> None:
    t1, t2 = _tool(400, "1"), _tool(40, "2")
    body = [
        _ai("a", inp=1000, out=50, model="m1"),
        t1,
        _ai("b", inp=5000, out=20, model="m2"),
        t2,
        _ai("c", inp=5300, out=10, model="m2"),
    ]
    seg = segment_tokens(None, body)
    # t1 was first read by the m2 request: an estimated share of its whole-context input.
    assert seg.messages[0] == MessageTokens(50, 50, "exact")  # a keeps its own output
    assert seg.messages[1].source == "estimated"
    want = (
        5000
        * estimate_message_tokens(t1)
        / (estimate_message_tokens(body[0]) + estimate_message_tokens(t1))
    )
    assert abs((seg.messages[1].context_tokens or 0) - want) <= 1
    # After the switch, intervals subtract as usual.
    assert seg.messages[3] == MessageTokens(5300 - 5000 - 20, None, "exact")
    assert seg.switches == (ModelSwitch(2, 5000),)


def test_context_before_a_switch_is_unchanged() -> None:
    body = [
        _ai("a", inp=1000, out=50, model="m1"),
        _tool(400, "1"),
        _ai("b", inp=1300, out=20, model="m1"),
        _tool(40, "2"),
        _ai("c", inp=9000, out=10, model="m2"),
    ]
    seg = segment_tokens(SYS, body)
    head_rec, records = context_through(SYS, body, seg, 2)  # the m1 request `b`
    assert head_rec == seg.head
    assert records == list(seg.messages[:2])


def test_context_after_a_switch_resplits_the_earlier_messages_by_the_new_total() -> None:
    body = [
        _ai("a", inp=1000, out=50, model="m1"),
        _tool(400, "1"),
        _ai("b", inp=5000, out=20, model="m2"),
        _tool(40, "2"),
        _ai("c", inp=5300, out=10, model="m2"),
    ]
    seg = segment_tokens(None, body)
    _, records = context_through(None, body, seg, 4)  # the request `c`
    assert sum(r.context_tokens or 0 for r in records[:2]) == 5000
    assert {r.source for r in records[:2]} == {"estimated"}
    # Messages from the switch on keep their values, so the context sums to c's input.
    assert sum(r.context_tokens or 0 for r in records) == 5300
    assert records[0].generation_tokens == 50


def test_same_model_keeps_exact() -> None:
    body = [
        _ai("a", inp=1000, out=50, model="m1"),
        _tool(400),
        _ai("b", inp=1300, out=20, model="m1"),
    ]
    assert segment_tokens(None, body).messages[1] == MessageTokens(250, None, "exact")


def test_ai_without_usage_merges_the_interval_forward() -> None:
    body = [
        _ai("a", inp=1000, out=50),
        _tool(400, "1"),
        _ai("x" * 80, inp=None, out=None),
        _tool(400, "2"),
        _ai("c", inp=2000, out=20),
    ]
    seg = segment_tokens(None, body)
    assert sum(m.context_tokens or 0 for m in seg.messages[1:4]) == 2000 - 1000 - 50
    assert {m.source for m in seg.messages[1:4]} == {"estimated"}
    assert seg.messages[2].generation_tokens is None


def test_leading_requests_without_usage_fold_into_the_head_anchor() -> None:
    human = _human(400)
    body = [human, _ai("x", inp=None, out=None), _tool(40), _ai("a", inp=800, out=5)]
    seg = segment_tokens(SYS, body)
    assert seg.head is not None
    assert (seg.head.context_tokens or 0) + _ctx(seg.messages, 0, 3) == 800
    assert seg.head.source == "estimated"


def test_tail_without_a_closing_request_is_not_in_context() -> None:
    body = [
        _ai("a", inp=1000, out=50),
        _tool(400),
        _ai("b", inp=1300, out=20),
        _tool(800),
        _human(40),
    ]
    seg = segment_tokens(None, body)
    assert seg.messages[2] == MessageTokens(20, 20, "exact")
    assert seg.messages[3] == MessageTokens(None, None, None)
    assert seg.messages[4] == MessageTokens(None, None, None)
    assert seg.last_input_tokens == 1300


def test_closing_request_anchors_the_tail() -> None:
    body = [_ai("a", inp=1000, out=50), _tool(40, "1"), _tool(80, "2")]
    seg = segment_tokens(None, body, ClosingRequest(input_tokens=1500))
    assert _ctx(seg.messages, 1, 3) == 1500 - 1000 - 50
    assert {m.source for m in seg.messages[1:]} == {"estimated"}


def test_closing_request_with_a_lone_tail_message_is_exact_unless_it_has_extra() -> None:
    body = [_ai("a", inp=1000, out=50), _tool(40)]
    exact = segment_tokens(None, body, ClosingRequest(input_tokens=1500))
    assert exact.messages[1] == MessageTokens(450, None, "exact")
    padded = segment_tokens(None, body, ClosingRequest(input_tokens=1500, extra_tokens=100))
    assert padded.messages[1] == MessageTokens(350, None, "estimated")


def test_closing_request_contradicting_the_chain_reanchors() -> None:
    body = [_ai("a", inp=1000, out=50), _tool(40)]
    seg = segment_tokens(None, body, ClosingRequest(input_tokens=900))
    assert _ctx(seg.messages, 0, 2) == 900


def test_segment_without_requests_has_no_context() -> None:
    seg = segment_tokens(SYS, [_human(40), _ai("x", inp=None, out=None)])
    assert seg.head == MessageTokens(None, None, None)
    assert seg.messages == (MessageTokens(None, None, None),) * 2
    assert seg.last_input_tokens is None


def test_closing_request_anchors_a_segment_that_had_no_requests() -> None:
    seg = segment_tokens(SYS, [_human(40)], ClosingRequest(input_tokens=600))
    assert seg.head is not None
    assert (seg.head.context_tokens or 0) + (seg.messages[0].context_tokens or 0) == 600


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
    history = FullHistory(
        [*seg0, *seg1], (head0, head1), (0, 2), (ClosingRequest(input_tokens=700), None)
    )
    segs = history_segment_tokens(history)
    assert len(segs) == 2
    assert segs[0].head is not None and segs[1].head is not None
    assert (segs[0].head.context_tokens or 0) + _ctx(segs[0].messages, 0, 1) == 500
    assert (segs[1].head.context_tokens or 0) + _ctx(segs[1].messages, 0, 1) == 900
    assert segs[1].messages[2] == MessageTokens(40, None, "exact")  # 950 - 900 - 10
    # Segment 0's closing request anchors its tail (the last AI is its own record).
    assert segs[0].messages[1] == MessageTokens(10, 10, "exact")
    assert segs[1].messages[3] == MessageTokens(5, 5, "exact")
    assert len(history_message_tokens(history)) == len(history.messages)


def test_summary_marks_estimated_when_any_part_is_estimated() -> None:
    exact = MessageTokens(10, None, "exact")
    assert total_of([exact, exact]).estimated is False
    assert total_of([exact, exact]).exact_fraction == 1.0
    mixed = total_of([exact, MessageTokens(30, None, "estimated")])
    assert mixed.estimated is True
    assert mixed.exact_fraction == 0.25
    assert total_of([]).estimated is False


def test_unread_messages_are_not_counted() -> None:
    unread = MessageTokens(None, None, None)
    total = total_of([MessageTokens(10, None, "exact"), unread])
    assert (total.tokens, total.estimated, total.exact_fraction) == (10, False, 1.0)


def test_segment_summary_carries_the_marker_and_unread_count() -> None:
    clean = single_segment_history([_ai("a", inp=500, out=5)])
    (summary,) = summarize_segments(clean)
    assert summary.estimated is False
    assert summary.unread_messages == 0
    open_tail = single_segment_history([_ai("a", inp=500, out=5), _tool(40)])
    (summary,) = summarize_segments(open_tail)
    assert summary.unread_messages == 1
    assert summary.context_tokens == 5
    (sealed,) = summarize_segments(
        open_tail._replace(segment_closings=(ClosingRequest(input_tokens=900),))
    )
    assert sealed.unread_messages == 0
    assert sealed.context_tokens == 900 - 500  # the AI's 5 plus the 395 the closing request adds


def test_summary_totals() -> None:
    body: list[BaseMessage] = [
        _human(1600),
        _ai("a", inp=1000, out=50),
        _tool(40),
        _ai("b", inp=1300, out=20),
    ]
    (summary,) = summarize_segments(single_segment_history([SYS, *body]))
    assert summary.message_count == 4
    assert summary.generation_tokens == 70
    assert summary.last_input_tokens == 1300
    assert summary.tokens_by_source["estimated"] == 1000  # head + human share the first request
    assert summary.messages_by_source == {"exact": 3, "estimated": 2}
    assert summary.estimated is True
    assert summary.context_tokens == sum(summary.tokens_by_source.values())


def test_empty_history_has_no_segments() -> None:
    assert history_segment_tokens(FullHistory([], (), ())) == ()
    assert history_message_tokens(FullHistory([], (), ())) == []


def test_cjk_costs_more_than_latin_and_whitespace_is_free() -> None:
    assert _est(HumanMessage(content=chr(0x4E2D) * 100)) > _est(HumanMessage(content="a" * 100)) * 2
    assert _est(HumanMessage(content="a b " * 10)) == _est(HumanMessage(content="ab" * 10))


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
    assert {p.source for p in parts.values()} == {"estimated"}
    assert parts["reasoning"].tokens > parts["output"].tokens
    est = ai_message_parts(msg, MessageTokens(200, 200, "estimated"))
    assert {p.source for p in est.values()} == {"estimated"}


def test_split_parts_lone_part_keeps_source_and_sections_conserve() -> None:
    assert split_parts({"only": "abc"}, 7, "exact") == {"only": PartTokens(7, "exact")}
    sections = split_parts({"a": "x" * 10, "b": "y" * 33, "c": ""}, 101, "exact")
    assert sum(p.tokens for p in sections.values()) == 101
    assert sections["c"].tokens == 0
