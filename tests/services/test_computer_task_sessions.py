"""Unit tests for services/computer/task_sessions.py — task-session envelope.

Covers start-on-first-action, action counting, and the lazy idle sweep that
emits computer_session_end. The daemon integration (note() wired into
_call_tool after touch) is covered in test_computer_mcp_daemon.py.
"""

from __future__ import annotations

import time

from services.computer.task_sessions import TaskSessionTracker


def _recorder() -> tuple[list[tuple[str, int, dict]], TaskSessionTracker]:  # pyright: ignore[reportMissingTypeArgument, reportUnknownParameterType]
    events: list[tuple[str, int, dict]] = []  # pyright: ignore[reportMissingTypeArgument, reportUnknownVariableType]

    def emit(event_type: str, agent_id: int, payload: dict) -> None:  # pyright: ignore[reportMissingTypeArgument, reportUnknownParameterType]
        events.append((event_type, agent_id, payload))  # pyright: ignore[reportUnknownMemberType]

    return events, TaskSessionTracker(idle_s=60.0)  # pyright: ignore[reportUnknownVariableType]


def test_first_action_emits_start_once() -> None:
    events, t = _recorder()  # pyright: ignore[reportUnknownVariableType]
    t.note(11, 7, "click", lambda *a: events.append(a))  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType, reportUnknownMemberType]
    t.note(11, 7, "type_text", lambda *a: events.append(a))  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType, reportUnknownMemberType]
    starts = [e for e in events if e[0] == "computer_session_start"]  # pyright: ignore[reportUnknownVariableType]
    assert len(starts) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert starts[0][1] == 7
    assert starts[0][2]["task_id"] == 11
    assert starts[0][2]["first_tool"] == "click"


def test_actions_count_within_session() -> None:
    events, t = _recorder()  # pyright: ignore[reportUnknownVariableType]
    t.note(11, 7, "click", lambda *a: events.append(a))  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType, reportUnknownMemberType]
    t.note(11, 7, "key", lambda *a: events.append(a))  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType, reportUnknownMemberType]
    t.note(11, 7, "key", lambda *a: events.append(a))  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType, reportUnknownMemberType]
    # still live: three actions, no end event yet
    assert not [e for e in events if e[0] == "computer_session_end"]  # pyright: ignore[reportUnknownVariableType]
    # the task goes idle past the threshold; the next note sweeps it
    t._sessions[11].last_action_at = time.time() - 61.0
    t.note(12, 8, "click", lambda *a: events.append(a))  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType, reportUnknownMemberType]
    ends = [e for e in events if e[0] == "computer_session_end"]  # pyright: ignore[reportUnknownVariableType]
    assert len(ends) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert ends[0][1] == 7
    assert ends[0][2]["task_id"] == 11
    assert ends[0][2]["action_count"] == 3
    assert ends[0][2]["outcome"] == "idle_timeout"


def test_idle_task_emits_end_on_next_note() -> None:
    events, t = _recorder()  # pyright: ignore[reportUnknownVariableType]
    t.note(11, 7, "click", lambda *a: events.append(a))  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType, reportUnknownMemberType]
    # task 11 goes idle (its last action is now > idle_s in the past)
    t._sessions[11].last_action_at = time.time() - 61.0
    t.note(12, 8, "click", lambda *a: events.append(a))  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType, reportUnknownMemberType]
    ends = [e for e in events if e[0] == "computer_session_end"]  # pyright: ignore[reportUnknownVariableType]
    assert len(ends) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert ends[0][2]["task_id"] == 11
    assert ends[0][2]["outcome"] == "idle_timeout"
    # task 11 is forgotten — a later action starts a fresh session
    t.note(11, 7, "click", lambda *a: events.append(a))  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType, reportUnknownMemberType]
    starts = [e for e in events if e[0] == "computer_session_start"]  # pyright: ignore[reportUnknownVariableType]
    assert len(starts) == 3  # 11, 12, then 11 again  # pyright: ignore[reportUnknownArgumentType]


def test_recent_task_not_swept() -> None:
    events, t = _recorder()  # pyright: ignore[reportUnknownVariableType]
    t.note(11, 7, "click", lambda *a: events.append(a))  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType, reportUnknownMemberType]
    t.note(12, 8, "click", lambda *a: events.append(a))  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType, reportUnknownMemberType]
    ends = [e for e in events if e[0] == "computer_session_end"]  # pyright: ignore[reportUnknownVariableType]
    assert ends == []
