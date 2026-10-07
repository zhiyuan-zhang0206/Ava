"""Chunk trigger rule, chunk cut and chunk location (pure; no database)."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from base.agents.history.checkpoint import FullHistory
from base.agents.history.hierarchy.chunks import (
    Chunk,
    ChunkDriftError,
    ChunkEmptyError,
    ChunkNotReadyError,
    ChunkTruncatedError,
    locate_chunk,
    message_time,
    plan_chunk,
    plan_closing_chunk,
)


def test_chunk_fires_once_tokens_grew_by_the_threshold() -> None:
    kwargs = {"cut_index": 1, "cut_tokens": 10_000, "request_len": 40, "threshold": 60_000}
    assert plan_chunk(input_tokens=69_999, **kwargs) is None
    assert plan_chunk(input_tokens=70_000, **kwargs) == Chunk(1, 40)


def test_chunk_is_measured_from_the_previous_cut_not_from_zero() -> None:
    first = plan_chunk(
        cut_index=0, cut_tokens=0, input_tokens=60_000, request_len=30, threshold=60_000
    )
    assert first == Chunk(0, 30)
    # The cut moved to (30, 60_000): 60k more tokens are needed for the next chunk.
    assert (
        plan_chunk(
            cut_index=30, cut_tokens=60_000, input_tokens=119_999, request_len=70, threshold=60_000
        )
        is None
    )
    assert plan_chunk(
        cut_index=30, cut_tokens=60_000, input_tokens=120_000, request_len=70, threshold=60_000
    ) == Chunk(30, 70)


def test_no_chunk_when_the_request_adds_nothing_past_the_cut() -> None:
    assert (
        plan_chunk(
            cut_index=30, cut_tokens=0, input_tokens=90_000, request_len=30, threshold=60_000
        )
        is None
    )


def test_closing_chunk_is_the_remainder_after_the_last_cut() -> None:
    assert plan_closing_chunk(cut_index=30, request_len=45) == Chunk(30, 45)
    assert plan_closing_chunk(cut_index=0, request_len=45) == Chunk(0, 45)
    assert plan_closing_chunk(cut_index=45, request_len=45) is None


def _msgs(n: int, prefix: str = "m") -> list[BaseMessage]:
    return [HumanMessage(content=f"{prefix}{i}", id=f"{prefix}{i}") for i in range(n)]


def _history() -> FullHistory:
    """Two segments: [head0, a0..a3] and [head1, b0..b2] stitched without the second head."""
    head0, head1 = SystemMessage(content="head0"), SystemMessage(content="head1")
    a, b = _msgs(4, "a"), _msgs(3, "b")
    return FullHistory([*a, *b], (head0, head1), (0, 4))


def test_live_chunk_in_the_newest_segment_maps_to_stitched_indices() -> None:
    # Request list of segment 1: [head1, b0, b1, b2]; chunk [1, 3) = b0, b1.
    located = locate_chunk(
        _history(), start_index=1, end_index=3, end_msg_id="b1", closing_segment=None
    )
    assert [m.id for m in located.messages] == ["b0", "b1"]
    assert located.prefix[0].content == "head1"
    assert located.span == (4, 5)  # stitched: a0..a3 = 0..3, b0 = 4


def test_chunk_starting_at_the_head_excludes_the_head() -> None:
    located = locate_chunk(
        _history(), start_index=0, end_index=3, end_msg_id="b1", closing_segment=None
    )
    assert [m.id for m in located.messages] == ["b0", "b1"]
    assert located.span == (4, 5)


def test_live_chunk_is_found_in_an_older_segment_by_its_end_id() -> None:
    located = locate_chunk(
        _history(), start_index=2, end_index=4, end_msg_id="a2", closing_segment=None
    )
    assert [m.id for m in located.messages] == ["a1", "a2"]
    assert located.prefix[0].content == "head0"
    assert located.span == (1, 2)


def test_live_chunk_beyond_the_checkpoint_is_not_ready() -> None:
    with pytest.raises(ChunkNotReadyError):
        locate_chunk(_history(), start_index=1, end_index=9, end_msg_id="b8", closing_segment=None)


def test_live_chunk_whose_end_id_moved_is_drift() -> None:
    # Index 2 now holds b1, not the recorded b0: a message was inserted before it.
    with pytest.raises(ChunkDriftError):
        locate_chunk(_history(), start_index=1, end_index=3, end_msg_id="b0", closing_segment=None)


def test_closing_chunk_is_read_from_its_segment_and_checked_at_its_end() -> None:
    # The boundary snapshot of segment 0 holds a0..a3; the job's end (index 5) is a3.
    located = locate_chunk(
        _history(), start_index=2, end_index=5, end_msg_id="a3", closing_segment=0
    )
    assert [m.id for m in located.messages] == ["a1", "a2", "a3"]
    assert located.span == (1, 3)


def test_a_closing_chunk_past_its_snapshot_is_truncated_not_silently_cut() -> None:
    # The job's end (index 9) lies past what the boundary snapshot persisted: the segment's last
    # turns are in no checkpoint, so the chunk is refused (the consumer fails the job and reports it).
    with pytest.raises(ChunkTruncatedError, match="last turns of the segment"):
        locate_chunk(_history(), start_index=2, end_index=9, end_msg_id="gone", closing_segment=0)


def test_a_closing_chunk_whose_end_id_moved_is_drift() -> None:
    with pytest.raises(ChunkDriftError):
        locate_chunk(_history(), start_index=2, end_index=5, end_msg_id="a2", closing_segment=0)


def test_closing_chunk_with_nothing_left_is_empty() -> None:
    with pytest.raises(ChunkEmptyError):
        locate_chunk(_history(), start_index=5, end_index=5, end_msg_id="a3", closing_segment=0)


def test_segment_without_a_head_keeps_indices_unshifted() -> None:
    history = FullHistory(_msgs(4, "a"), (None,), (0,))
    located = locate_chunk(
        history, start_index=1, end_index=3, end_msg_id="a2", closing_segment=None
    )
    assert [m.id for m in located.messages] == ["a1", "a2"]
    assert located.span == (1, 2)


def test_message_time_takes_the_first_and_last_stamped_message() -> None:
    def stamped(content: str, at: str) -> BaseMessage:
        return AIMessage(content=content, additional_kwargs={"ava_created_at": at})

    msgs = [
        HumanMessage(content="legacy"),
        stamped("a", "2026-10-05T01:00:00+00:00"),
        stamped("b", "2026-10-05T02:00:00+00:00"),
        HumanMessage(content="legacy tail"),
    ]
    first, last = message_time(msgs, last=False), message_time(msgs, last=True)
    assert first is not None and first.hour == 1
    assert last is not None and last.hour == 2
    assert message_time(msgs[:1], last=False) is None
