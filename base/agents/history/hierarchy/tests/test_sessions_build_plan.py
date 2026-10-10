"""Sessions of a stitched history, their coverage, and the chunk jobs a build would enqueue."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from base.agents.history.checkpoint import FullHistory
from base.agents.history.hierarchy.build import (
    OUTPUT_TOKENS_PER_JOB,
    estimate_cost,
    plan_jobs,
)
from base.agents.history.hierarchy.sessions import (
    SessionBoundaryError,
    build_sessions,
    coverage_of,
)
from base.agents.history.hierarchy.units import read_times
from base.agents.history.timeline_inputs import TimelineReadInputs
from base.clock import Clock
from base.config import settings
from base.lm.catalog import ModelCatalog
from base.lm.pricing import quote

_TIMELINE_INPUTS = TimelineReadInputs(
    Clock.from_settings, lambda: settings.general.message_timestamps
)

_T0 = datetime(2026, 10, 5, tzinfo=UTC)
_THRESHOLD = 1000


def _turns(tag: str, count: int, *, first_hour: int) -> list[BaseMessage]:
    """`count` turns (an inbound, then an AI turn whose input grows by 400 tokens), one per minute."""
    out: list[BaseMessage] = []
    for i in range(count):
        stamp = (_T0 + timedelta(hours=first_hour, minutes=i)).isoformat()
        out.append(
            HumanMessage(
                content=f"{tag}{i}",
                id=f"{tag}h{i}",
                additional_kwargs={
                    "ava_msg_type": "inbound",
                    "ava_source": "user",
                    "ava_created_at": stamp,
                },
            )
        )
        tokens = 2000 + 400 * i
        out.append(
            AIMessage(
                content="ok",
                id=f"{tag}a{i}",
                additional_kwargs={"ava_created_at": stamp},
                usage_metadata={
                    "input_tokens": tokens,
                    "output_tokens": 10,
                    "total_tokens": tokens + 10,
                },
            )
        )
    return out


def _history(*counts: int) -> FullHistory:
    """One session per entry (`counts` turns each); heads are one SystemMessage each."""
    body: list[BaseMessage] = []
    starts: list[int] = []
    for k, count in enumerate(counts):
        starts.append(len(body))
        body.extend(_turns(f"s{k}", count, first_hour=k * 3))
    return FullHistory(body, tuple(SystemMessage(content="head") for _ in counts), tuple(starts))


def _sessions(history: FullHistory, *boundaries: str):
    return build_sessions(
        history, list(boundaries), read_times(history.messages, timeline_inputs=_TIMELINE_INPUTS)
    )


def test_sessions_are_numbered_from_the_oldest_and_only_the_last_is_in_progress() -> None:
    history = _history(6, 3, 4)
    sessions = _sessions(history, "cp-a", "cp-b")
    assert [s.number for s in sessions] == [1, 2, 3]
    assert [s.boundary_checkpoint_id for s in sessions] == ["cp-a", "cp-b", None]
    assert [s.messages for s in sessions] == [12, 6, 8]
    assert [(s.first, s.end) for s in sessions] == [(0, 12), (12, 18), (18, 26)]
    # Peak: the largest provider-reported input of any AI turn of the session.
    assert [s.peak_input_tokens for s in sessions] == [
        2000 + 400 * 5,
        2000 + 400 * 2,
        2000 + 400 * 3,
    ]
    # Times are read times: the first and last message of the session.
    assert sessions[0].start == _T0 and sessions[0].ended == _T0 + timedelta(minutes=5)
    assert sessions[2].start == _T0 + timedelta(hours=6)


def test_a_history_closed_by_its_last_compaction_has_no_session_in_progress() -> None:
    history = _history(3, 3)
    assert [s.boundary_checkpoint_id for s in _sessions(history, "cp-a", "cp-b")] == [
        "cp-a",
        "cp-b",
    ]


def test_segments_and_boundaries_that_do_not_line_up_are_refused() -> None:
    with pytest.raises(SessionBoundaryError):
        _sessions(_history(3, 3), "cp-a", "cp-b", "cp-c")
    with pytest.raises(SessionBoundaryError):
        _sessions(_history(3, 3, 3), "cp-a")


def test_coverage_is_none_partial_or_full_by_the_level_one_spans() -> None:
    history = _history(6, 3)
    first, live = _sessions(history, "cp-a")
    assert first.material == (0, 11) and live.material == (12, 17)
    none = coverage_of(history, first, [], timeline_inputs=_TIMELINE_INPUTS)
    assert (none.status, none.ratio, none.covered_messages, none.total_messages) == (
        "none",
        0.0,
        0,
        12,
    )
    part = coverage_of(history, first, [(0, 5)], timeline_inputs=_TIMELINE_INPUTS)
    assert (part.status, part.covered_messages) == ("partial", 6) and part.ratio == 0.5
    assert (
        coverage_of(history, first, [(0, 5), (6, 11)], timeline_inputs=_TIMELINE_INPUTS).status
        == "full"
    )
    # Nodes of another session do not touch this one.
    assert (
        coverage_of(history, first, [(12, 17)], timeline_inputs=_TIMELINE_INPUTS).status == "none"
    )


def test_a_run_of_framework_notes_alone_is_not_missing() -> None:
    note = HumanMessage(content="n", id="note", additional_kwargs={"ava_msg_type": "system_note"})
    messages = [*_turns("s", 2, first_hour=0), note]
    history = FullHistory(messages, (SystemMessage(content="head"),), (0,))
    (session,) = _sessions(history)
    assert session.material == (0, 4)
    assert (
        coverage_of(history, session, [(0, 3)], timeline_inputs=_TIMELINE_INPUTS).status == "full"
    )


def test_the_jobs_follow_the_live_rule_and_end_in_the_sessions_remainder() -> None:
    history = _history(6, 3)
    first, live = _sessions(history, "cp-a")
    jobs = plan_jobs(
        history, [first, live], [], threshold=_THRESHOLD, timeline_inputs=_TIMELINE_INPUTS
    )
    # Session 1: a chunk when the input has grown by 1000 (turn 3), then the closing remainder.
    # Request indices count the SystemMessage head at 0; stitched = request - 1.
    assert [(j.session, j.start_index, j.end_index) for j in jobs if j.session == 1] == [
        (1, 1, 8),
        (1, 8, 13),
    ]
    assert [(j.first_message, j.last_message) for j in jobs if j.session == 1] == [(0, 6), (7, 11)]
    # A closed session names its boundary checkpoint, the one in progress none; the end message
    # is the one the consumer later verifies.
    assert {j.boundary_checkpoint_id for j in jobs if j.session == 1} == {"cp-a"}
    assert [j.end_msg_id for j in jobs if j.session == 1] == ["s0h3", "s0a5"]
    assert all(j.boundary_checkpoint_id is None for j in jobs if j.session == 2)
    # The session in progress is built to its last request: one closing job.
    assert [(j.start_index, j.end_index, j.end_msg_id) for j in jobs if j.session == 2] == [
        (1, 7, "s1a2")
    ]
    assert [j.compact_version for j in jobs] == [0, 0, 1]


def test_covered_runs_are_skipped_exactly() -> None:
    history = _history(6, 3)
    first, _ = _sessions(history, "cp-a")
    # The first job's stretch (0..6) is described but for its tail; the second's (7..11) is not.
    jobs = plan_jobs(
        history, [first], [(0, 4)], threshold=_THRESHOLD, timeline_inputs=_TIMELINE_INPUTS
    )
    assert [(j.first_message, j.last_message) for j in jobs] == [(5, 6), (7, 11)]
    # A node in the middle of a chunk cuts it into the runs on both sides, one job each.
    jobs = plan_jobs(
        history, [first], [(2, 3)], threshold=_THRESHOLD, timeline_inputs=_TIMELINE_INPUTS
    )
    assert [(j.first_message, j.last_message) for j in jobs] == [(0, 1), (4, 6), (7, 11)]
    assert [(j.start_index, j.end_index) for j in jobs] == [(1, 3), (5, 8), (8, 13)]
    # Fully covered: nothing to build.
    assert (
        plan_jobs(
            history, [first], [(0, 11)], threshold=_THRESHOLD, timeline_inputs=_TIMELINE_INPUTS
        )
        == []
    )


def test_the_estimate_prices_a_cold_prefix_plus_cached_rereads(
    *, model_catalog: ModelCatalog
) -> None:
    history = _history(6)
    (session,) = _sessions(history)
    jobs = plan_jobs(history, [session], [], threshold=_THRESHOLD, timeline_inputs=_TIMELINE_INPUTS)
    # The first job ends before the AI turn that follows it: that turn's reported input is its prefix.
    assert [j.input_tokens for j in jobs] == [3200, 4000 + 10]
    estimate = estimate_cost("deepseek-v4-flash", jobs, prices=model_catalog.prices)
    assert estimate.jobs == 2 and estimate.output_tokens == 2 * OUTPUT_TOKENS_PER_JOB
    cold = [
        quote(
            "deepseek-v4-flash",
            j.input_tokens,
            OUTPUT_TOKENS_PER_JOB,
            0,
            prices=model_catalog.prices,
        )
        for j in jobs
    ]
    warm = [
        quote("deepseek-v4-flash", j.input_tokens, 0, j.input_tokens, prices=model_catalog.prices)
        for j in jobs
    ]
    assert estimate.cost_usd == pytest.approx(
        sum(c.cost_usd + 0.85 * w.cost_usd for c, w in zip(cold, warm, strict=True))  # type: ignore[union-attr]
    )
    assert estimate_cost("no-such-model", jobs, prices=model_catalog.prices).cost_usd is None
