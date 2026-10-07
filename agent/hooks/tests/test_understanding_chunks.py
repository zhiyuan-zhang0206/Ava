# pyright: reportUnknownMemberType = warning
# pyright: reportUnknownArgumentType = warning
# pyright: reportUnknownLambdaType = warning
# pyright: reportUnknownVariableType = warning
"""When the llm node and the compact paths enqueue understanding chunks."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage

from agent.hooks import understanding_chunks as uc
from agent.state_channels import CompactState
from base.agents.history.hierarchy.chunks import Chunk
from base.config import settings


class _Queue:
    """Stands in for `enqueue_chunk`: records calls, optionally refuses."""

    def __init__(self, ok: bool = True) -> None:
        self.ok, self.calls = ok, []

    async def __call__(self, pool: Any, agent_id: int, **kwargs: Any) -> bool:
        self.calls.append((agent_id, kwargs))
        return self.ok


def _request(n: int) -> list[AnyMessage]:
    return [
        SystemMessage(content="head", id="h"),
        *(HumanMessage(content=str(i), id=f"m{i}") for i in range(1, n)),
    ]


def _reply(input_tokens: int) -> AIMessage:
    return AIMessage(
        content="x",
        usage_metadata={
            "input_tokens": input_tokens,
            "output_tokens": 1,
            "total_tokens": input_tokens + 1,
        },
    )


# A segment past its first turn: cut just after the head, 500 tokens of head.
_BASELINED = CompactState(understanding_cut_index=1, understanding_cut_tokens=500)


@pytest.fixture(autouse=True)
def _enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.agent, "understanding_enabled", True)
    monkeypatch.setattr(settings.agent, "understanding_chunk_tokens", 1000)


async def test_turn_below_the_threshold_enqueues_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    queue = _Queue()
    monkeypatch.setattr(uc, "enqueue_chunk", queue)
    update = await uc.due_chunk_update(
        _BASELINED, _request(10), _reply(1499), pool=MagicMock(), agent_id=3
    )
    assert update == {} and queue.calls == []


async def test_a_segments_first_turn_only_records_the_baseline_past_the_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = _Queue()
    monkeypatch.setattr(uc, "enqueue_chunk", queue)
    fresh = await uc.due_chunk_update(
        CompactState(), _request(2), _reply(30_000), pool=MagicMock(), agent_id=3
    )
    assert queue.calls == []
    assert (
        fresh["compact"].understanding_cut_index,
        fresh["compact"].understanding_cut_tokens,
    ) == (
        1,
        30_000,
    )
    compacted: list[AnyMessage] = [
        SystemMessage(content="head", id="h"),
        HumanMessage(
            content="summary", id="s", additional_kwargs={"ava_msg_type": "compact_summary"}
        ),
        HumanMessage(content="next", id="n"),
    ]
    after = await uc.due_chunk_update(
        CompactState(version=2), compacted, _reply(40_000), pool=MagicMock(), agent_id=3
    )
    assert after["compact"].understanding_cut_index == 2


async def test_turn_past_the_threshold_enqueues_and_moves_the_cut(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = _Queue()
    monkeypatch.setattr(uc, "enqueue_chunk", queue)
    compact = _BASELINED.model_copy(update={"version": 4})
    update = await uc.due_chunk_update(
        compact, _request(10), _reply(1500), pool=MagicMock(), agent_id=3
    )
    assert queue.calls == [
        (3, {"compact_version": 4, "chunk": Chunk(1, 10), "end_msg_id": "m9"}),
    ]
    moved = update["compact"]
    assert (moved.understanding_cut_index, moved.understanding_cut_tokens, moved.version) == (
        10,
        1500,
        4,
    )
    # The next chunk needs another threshold of growth past the new cut.
    assert (
        await uc.due_chunk_update(moved, _request(14), _reply(2499), pool=MagicMock(), agent_id=3)
        == {}
    )


async def test_failed_enqueue_keeps_the_cut_so_the_stretch_is_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(uc, "enqueue_chunk", _Queue(ok=False))
    assert (
        await uc.due_chunk_update(
            _BASELINED, _request(10), _reply(5000), pool=MagicMock(), agent_id=3
        )
        == {}
    )


async def test_disabled_or_poolless_turns_do_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    queue = _Queue()
    monkeypatch.setattr(uc, "enqueue_chunk", queue)
    assert (
        await uc.due_chunk_update(CompactState(), _request(10), _reply(5000), pool=None, agent_id=3)
        == {}
    )
    monkeypatch.setattr(settings.agent, "understanding_enabled", False)
    assert (
        await uc.due_chunk_update(
            _BASELINED, _request(10), _reply(5000), pool=MagicMock(), agent_id=3
        )
        == {}
    )
    assert queue.calls == []


async def test_turn_without_usage_never_triggers(monkeypatch: pytest.MonkeyPatch) -> None:
    queue = _Queue()
    monkeypatch.setattr(uc, "enqueue_chunk", queue)
    assert (
        await uc.due_chunk_update(
            CompactState(), _request(10), AIMessage(content="x"), pool=MagicMock(), agent_id=3
        )
        == {}
    )


async def test_compaction_enqueues_the_remainder_against_the_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = _Queue()
    monkeypatch.setattr(uc, "enqueue_chunk", queue)
    compact = CompactState(version=2, understanding_cut_index=6, understanding_cut_tokens=900)
    await uc.enqueue_closing_chunk(
        compact, _request(12), pool=MagicMock(), agent_id=3, boundary="cp-9"
    )
    assert queue.calls == [
        (
            3,
            {
                "compact_version": 2,
                "chunk": Chunk(6, 12),
                "end_msg_id": "m11",
                "boundary_checkpoint_id": "cp-9",
            },
        ),
    ]


async def test_compaction_without_a_remainder_or_a_boundary_enqueues_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = _Queue()
    monkeypatch.setattr(uc, "enqueue_chunk", queue)
    at_end = CompactState(understanding_cut_index=12)
    await uc.enqueue_closing_chunk(
        at_end, _request(12), pool=MagicMock(), agent_id=3, boundary="cp-9"
    )
    await uc.enqueue_closing_chunk(
        CompactState(), _request(12), pool=MagicMock(), agent_id=3, boundary=None
    )
    assert queue.calls == []


def test_compaction_resets_the_cut_with_the_version() -> None:
    nxt = CompactState(
        version=2, understanding_cut_index=40, understanding_cut_tokens=70_000, reminder_shown=True
    ).next_segment()
    assert (nxt.version, nxt.understanding_cut_index, nxt.understanding_cut_tokens) == (3, 0, 0)
    assert nxt.reminder_shown  # the other bookkeeping rides along untouched


async def test_stamp_enqueues_the_closing_chunk_against_the_stamped_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent.hooks import compact
    from agent.state import AgentState

    queue = _Queue()
    monkeypatch.setattr(uc, "enqueue_chunk", queue)

    async def stamp(_pool: object, _thread_id: str) -> str:
        return "cp-7"

    monkeypatch.setattr(compact, "mark_compact_boundary", stamp)
    state = AgentState(
        messages=_request(8), compact=CompactState(version=1, understanding_cut_index=3)
    )
    await compact.stamp_compact_boundary(MagicMock(), 9, state)
    assert queue.calls == [
        (
            9,
            {
                "compact_version": 1,
                "chunk": Chunk(3, 8),
                "end_msg_id": "m7",
                "boundary_checkpoint_id": "cp-7",
            },
        )
    ]


async def test_failed_stamp_enqueues_no_closing_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent.hooks import compact
    from agent.state import AgentState

    queue = _Queue()
    monkeypatch.setattr(uc, "enqueue_chunk", queue)

    async def stamp(_pool: object, _thread_id: str) -> str:
        raise RuntimeError("db down")

    monkeypatch.setattr(compact, "mark_compact_boundary", stamp)
    await compact.stamp_compact_boundary(MagicMock(), 9, AgentState(messages=_request(8)))
    assert queue.calls == []


async def test_closing_chunk_stops_before_an_unanswered_tool_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = _Queue()
    monkeypatch.setattr(uc, "enqueue_chunk", queue)
    open_call = AIMessage(
        content="", id="call", tool_calls=[{"name": "execute_code", "args": {}, "id": "t1"}]
    )
    await uc.enqueue_closing_chunk(
        CompactState(version=2, understanding_cut_index=3, understanding_cut_tokens=900),
        [*_request(8), open_call],
        pool=MagicMock(),
        agent_id=3,
        boundary="cp-9",
    )
    assert queue.calls == [
        (
            3,
            {
                "compact_version": 2,
                "chunk": Chunk(3, 8),
                "end_msg_id": "m7",
                "boundary_checkpoint_id": "cp-9",
            },
        )
    ]


def _framework(kind: str, id_: str) -> HumanMessage:
    return HumanMessage(content=f"[system] {id_}", id=id_, additional_kwargs={"ava_msg_type": kind})


def _new_agent_segment() -> list[AnyMessage]:
    return [
        SystemMessage(content="prompt", id="sys"),
        _framework("system_note", "timeout"),
        _framework("system_note", "tz"),
        _framework("system_note", "agent-id"),
        _framework("system_note", "memory-index"),
        HumanMessage(content="first task", id="u1", additional_kwargs={"ava_msg_type": "inbound"}),
        AIMessage(content="ok", id="a1"),
    ]


def _compacted_segment() -> list[AnyMessage]:
    return [
        SystemMessage(content="prompt", id="sys"),
        _framework("system_note", "timeout"),
        _framework("system_note", "tz"),
        _framework("system_note", "agent-id"),
        _framework("system_note", "memory-index"),
        _framework("compact_summary", "summary"),
        _framework("system_note", "dump-path"),
        AIMessage(content="continuing", id="a1"),
        HumanMessage(content="later", id="u2", additional_kwargs={"ava_msg_type": "inbound"}),
    ]


def test_head_of_a_new_agent_ends_at_the_first_user_message() -> None:
    assert uc.segment_head_len(_new_agent_segment()) == 5


def test_head_of_a_compacted_segment_includes_summary_and_dump_note() -> None:
    assert uc.segment_head_len(_compacted_segment()) == 7


def test_head_stops_at_the_first_non_framework_message_and_edge_cases() -> None:
    assert uc.segment_head_len([]) == 0
    assert uc.segment_head_len(_request(3)) == 1
    # a framework note later in the segment is material, not head
    later = [*_new_agent_segment(), _framework("system_note", "later")]
    assert uc.segment_head_len(later) == 5
    request = _framework("compact_request", "req")
    assert uc.segment_head_len([SystemMessage(content="p"), request]) == 2


async def test_first_turn_baseline_skips_the_whole_compacted_head() -> None:
    update = await uc.due_chunk_update(
        CompactState(version=2), _compacted_segment(), _reply(40_000), pool=MagicMock(), agent_id=3
    )
    assert update["compact"].understanding_cut_index == 7


async def test_closing_chunk_of_a_compacted_segment_starts_past_the_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = _Queue()
    monkeypatch.setattr(uc, "enqueue_chunk", queue)
    await uc.enqueue_closing_chunk(
        CompactState(version=2),
        _compacted_segment(),
        pool=MagicMock(),
        agent_id=3,
        boundary="cp-1",
    )
    assert queue.calls[0][1]["chunk"] == Chunk(7, 9)
    queue.calls.clear()
    # a segment that never left its head has nothing to close
    await uc.enqueue_closing_chunk(
        CompactState(version=0),
        _new_agent_segment()[:5],
        pool=MagicMock(),
        agent_id=3,
        boundary="cp-0",
    )
    assert queue.calls == []


class _State:
    def __init__(self, messages: list[AnyMessage]) -> None:
        self.messages = messages


def _stamped(seconds: int) -> HumanMessage:
    return HumanMessage(
        content="m",
        id=f"s{seconds}",
        additional_kwargs={"ava_created_at": f"2026-10-07T12:00:{seconds:02d}+00:00"},
    )


def _checkpoint_clock(monkeypatch: pytest.MonkeyPatch, seconds: list[int | None]) -> list[int]:
    """The newest checkpoint's time per poll (seconds past 12:00:00); the polls made."""
    polls: list[int] = []

    async def newest(_pool: Any, _agent_id: int) -> datetime | None:
        polls.append(1)
        sec = seconds.pop(0) if len(seconds) > 1 else seconds[0]
        return None if sec is None else datetime(2026, 10, 7, 12, 0, sec, tzinfo=UTC)

    monkeypatch.setattr(uc, "_newest_checkpoint_ts", newest)
    monkeypatch.setattr(settings.agent, "understanding_enabled", True)
    monkeypatch.setattr(uc, "_SNAPSHOT_POLL_SECONDS", 0.0)
    return polls


async def test_the_boundary_waits_for_a_checkpoint_as_new_as_the_states_last_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The graph persists a super-step's checkpoint asynchronously, so the newest row can be one
    step behind the state when a compaction stamps its boundary: the stamp waits for it."""
    polls = _checkpoint_clock(monkeypatch, [5, 9, 20])  # the last message is from second 10
    emitted: list[str] = []
    monkeypatch.setattr(uc.telemetry, "emit", lambda _k, name, **_kw: emitted.append(name))
    await uc.await_snapshot(MagicMock(), _State([*_request(3), _stamped(10)]), 3)
    assert len(polls) == 3 and emitted == []


async def test_a_snapshot_that_never_catches_up_is_stamped_anyway_with_an_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _checkpoint_clock(monkeypatch, [5])
    monkeypatch.setattr(uc, "SNAPSHOT_WAIT_SECONDS", 0.0)
    events: list[tuple[str, dict[str, Any] | None]] = []
    monkeypatch.setattr(
        uc.telemetry, "emit", lambda _k, name, attributes=None: events.append((name, attributes))
    )
    await uc.await_snapshot(MagicMock(), _State([*_request(3), _stamped(10)]), 3)
    assert [e[0] for e in events] == ["understanding_snapshot_lag"]
    attrs = events[0][1]
    assert attrs is not None and attrs["agent_id"] == 3


async def test_nothing_is_read_when_understanding_is_off_or_there_is_nothing_to_compare(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    polls = _checkpoint_clock(monkeypatch, [None])
    monkeypatch.setattr(settings.agent, "understanding_enabled", False)
    await uc.await_snapshot(MagicMock(), _State([_stamped(10)]), 3)
    monkeypatch.setattr(settings.agent, "understanding_enabled", True)
    await uc.await_snapshot(MagicMock(), None, 3)
    await uc.await_snapshot(None, _State([_stamped(10)]), 3)
    await uc.await_snapshot(MagicMock(), _State(_request(3)), 3)  # the last message has no time
    assert polls == []
