from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from services.idle_shell_reminder import daemon as reminder_daemon
from services.idle_shell_reminder.engine import (
    THRESHOLDS_S,
    IdleObservation,
    OwnerReminder,
    SessionState,
    advance,
    record_reminder,
)
from services.idle_shell_reminder.state import load_state, save_state


def _observation(
    name: str = "ava-agent-7-shell-3-worker",
    *,
    owner: int = 7,
    sdk_id: int = 3,
    idle_start: float | None = 1_000.0,
) -> IdleObservation:
    return IdleObservation(name=name, owner=owner, sdk_id=sdk_id, idle_start=idle_start)


def _advance(
    state: dict[str, SessionState],
    now: float,
    observations: tuple[IdleObservation, ...],
    *,
    alive: bool = True,
    retained: set[int] | None = None,
) -> tuple[dict[str, SessionState], tuple[OwnerReminder, ...]]:
    return advance(
        state,
        now=now,
        observations=observations,
        live_session_names={observation.name for observation in observations},
        owner_alive=lambda _owner: alive,
        retained_reply_ids=lambda _owner, _ids: retained or set(),
    )


def test_observations_exempt_daemon_owned_page_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    page = "ava-agent-7-shell-3-page-dashboard"
    worker = "ava-agent-7-shell-4-worker"
    requests: list[str] = []
    monkeypatch.setattr(
        reminder_daemon.shared.pty_sessions.cli,
        "live_sessions",
        lambda: {page: object(), worker: object()},
    )

    def _request(name: str, _request: dict[str, str]) -> dict[str, object]:
        requests.append(name)
        return {"ok": True, "data": {"idle": False, "idle_since": 0.0}}

    monkeypatch.setattr(reminder_daemon.shared.pty_sessions.cli, "session_request", _request)
    observations, live_names = reminder_daemon._observations()

    assert [observation.name for observation in observations] == [worker]
    assert live_names == {worker}
    assert requests == [worker]


def test_fires_at_each_backoff_threshold_and_not_just_before() -> None:
    observation = _observation()
    state: dict[str, SessionState] = {}
    baseline = observation.idle_start
    assert baseline is not None

    for level, threshold in enumerate(THRESHOLDS_S):
        state, reminders = _advance(state, baseline + threshold - 0.001, (observation,))
        assert reminders == (), f"level {level} fired before its threshold"

        due_at = baseline + threshold
        state, reminders = _advance(state, due_at, (observation,))
        assert len(reminders) == 1, f"level {level} did not fire at its threshold"
        reminder = reminders[0]
        record_reminder(state, reminder, inbound_id=100 + level, reminded_at=due_at)
        assert state[observation.name].level == min(level + 1, len(THRESHOLDS_S) - 1)
        baseline = due_at


def test_last_level_keeps_firing_every_24_hours() -> None:
    observation = _observation()
    state = {
        observation.name: SessionState(
            owner=observation.owner,
            idle_start=observation.idle_start,
            level=len(THRESHOLDS_S) - 1,
            exempt=False,
            last_reminded_at=10_000.0,
            last_reminder_inbound_id=80,
        )
    }

    state, reminders = _advance(state, 10_000.0 + THRESHOLDS_S[-1], (observation,))
    assert len(reminders) == 1
    record_reminder(state, reminders[0], inbound_id=81, reminded_at=96_400.0)
    assert state[observation.name].level == len(THRESHOLDS_S) - 1

    state, reminders = _advance(state, 96_400.0 + THRESHOLDS_S[-1] - 0.001, (observation,))
    assert reminders == ()
    state, reminders = _advance(state, 96_400.0 + THRESHOLDS_S[-1], (observation,))
    assert len(reminders) == 1


def test_busy_and_new_idle_output_reset_the_idle_period() -> None:
    observation = _observation()
    state, reminders = _advance({}, 1_300.0, (observation,))
    assert len(reminders) == 1
    record_reminder(state, reminders[0], inbound_id=90, reminded_at=1_300.0)

    busy = _observation(idle_start=None)
    state, reminders = _advance(state, 1_301.0, (busy,))
    assert reminders == ()
    assert state[observation.name].idle_start is None
    assert state[observation.name].level == 0

    newly_idle = _observation(idle_start=2_000.0)
    state, reminders = _advance(state, 2_299.999, (newly_idle,))
    assert reminders == ()
    state, reminders = _advance(state, 2_300.0, (newly_idle,))
    assert len(reminders) == 1
    record_reminder(state, reminders[0], inbound_id=91, reminded_at=2_300.0)

    fresh_prompt = _observation(idle_start=2_301.0)
    state, reminders = _advance(state, 2_600.999, (fresh_prompt,))
    assert reminders == ()
    assert state[observation.name].level == 0
    assert state[observation.name].idle_start == 2_301.0


def test_keep_reply_exempts_every_session_covered_by_merged_reminder() -> None:
    observations = (
        _observation("ava-agent-7-shell-3-worker", sdk_id=3),
        _observation("ava-agent-7-shell-4-server", sdk_id=4),
    )
    state, reminders = _advance({}, 1_300.0, observations)
    assert len(reminders) == 1
    record_reminder(state, reminders[0], inbound_id=111, reminded_at=1_300.0)

    calls: list[tuple[int, frozenset[int]]] = []

    def _retained(owner: int, inbound_ids: frozenset[int]) -> set[int]:
        calls.append((owner, inbound_ids))
        return {111}

    state, reminders = advance(
        state,
        now=10_000.0,
        observations=observations,
        live_session_names={observation.name for observation in observations},
        owner_alive=lambda _owner: True,
        retained_reply_ids=_retained,
    )

    assert reminders == ()
    assert calls == [(7, frozenset({111}))], "checkpoint must be scanned once per owner per tick"
    assert all(session.exempt for session in state.values())


def test_due_sessions_of_one_owner_merge_into_one_message() -> None:
    observations = (
        _observation("ava-agent-7-shell-3-worker", sdk_id=3),
        _observation("ava-agent-7-shell-4-server", sdk_id=4, idle_start=900.0),
    )

    _state, reminders = _advance({}, 1_300.0, observations)

    assert len(reminders) == 1
    reminder = reminders[0]
    assert reminder.owner == 7
    assert {session.sdk_id for session in reminder.sessions} == {3, 4}
    assert "ava-agent-7-shell-3-worker" in reminder.content
    assert "ava-agent-7-shell-4-server" in reminder.content
    assert "ava.shell.sessions.kill(3)" in reminder.content
    assert "ava.shell.sessions.kill(4)" in reminder.content
    assert reminder.content.count("回复『保留』") == 1


def test_terminated_owner_is_skipped_without_advancing_backoff() -> None:
    observation = _observation()

    state, reminders = _advance({}, 10_000.0, (observation,), alive=False)

    assert reminders == ()
    assert state[observation.name].level == 0
    assert state[observation.name].last_reminded_at is None


def test_state_round_trip_and_dead_session_cleanup(tmp_path: Path) -> None:
    path = tmp_path / "idle_shell_reminder.json"
    session_name = "ava-agent-7-shell-3-worker"
    original = {
        session_name: SessionState(
            owner=7,
            idle_start=500.25,
            level=4,
            exempt=True,
            last_reminded_at=700.5,
            last_reminder_inbound_id=222,
        )
    }

    save_state(path, original)
    loaded = load_state(path)

    assert loaded == original
    loaded, reminders = advance(
        loaded,
        now=1_000.0,
        observations=(),
        live_session_names=set(),
        owner_alive=lambda _owner: True,
        retained_reply_ids=lambda _owner, _ids: set(),
    )
    assert loaded == {}
    assert reminders == ()


def test_session_already_idle_three_hours_fires_then_waits_thirty_minutes() -> None:
    observation = _observation(idle_start=1_000.0)
    idle_start = observation.idle_start
    assert idle_start is not None
    daemon_start = idle_start + 3 * 60 * 60

    state, reminders = _advance({}, daemon_start, (observation,))
    assert len(reminders) == 1
    record_reminder(state, reminders[0], inbound_id=333, reminded_at=daemon_start)

    state, reminders = _advance(state, daemon_start + 30 * 60 - 0.001, (observation,))
    assert reminders == ()
    state, reminders = _advance(state, daemon_start + 30 * 60, (observation,))
    assert len(reminders) == 1


@pytest.mark.parametrize("idle_start", [float("nan"), float("inf"), -float("inf")])
def test_non_finite_observation_is_treated_as_busy(idle_start: float) -> None:
    observation = _observation(idle_start=idle_start)

    state, reminders = _advance({}, 10_000.0, (observation,))

    assert reminders == ()
    assert state[observation.name].idle_start is None


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("ava-agent-7-shell-3", (7, 3)),
        ("ava-agent-42-shell-19-dev-server", (42, 19)),
        ("ava-agent-x-shell-3", None),
        ("ava-agent-7-watcher-3", None),
        ("other-agent-7-shell-3", None),
    ],
)
def test_session_name_parser_extracts_owner_and_sdk_id(
    name: str, expected: tuple[int, int] | None
) -> None:
    assert reminder_daemon._parse_session_name(name) == expected


def test_keep_reply_scan_anchors_on_latest_matching_inbound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages: list[BaseMessage] = [
        HumanMessage(content="old reminder", additional_kwargs={"ava_inbound_id": 40}),
        AIMessage(content="保留"),
        HumanMessage(content="new reminder", additional_kwargs={"ava_inbound_id": 41}),
        AIMessage(content="继续提醒"),
    ]

    def _messages(_owner: int) -> list[BaseMessage]:
        return messages

    monkeypatch.setattr("shared.checkpoint.load_checkpoint_messages", _messages)

    assert reminder_daemon._retained_reply_ids(7, frozenset({40, 41})) == set()

    messages.append(AIMessage(content=[{"type": "text", "text": "请保留这个 shell"}]))
    assert reminder_daemon._retained_reply_ids(7, frozenset({40, 41})) == {41}


def test_checkpoint_read_error_is_tolerated(monkeypatch: pytest.MonkeyPatch) -> None:
    def _unreadable(_owner: int) -> list[object]:
        raise reminder_daemon.shared.checkpoint.CheckpointReadError("database unavailable")

    monkeypatch.setattr("shared.checkpoint.load_checkpoint_messages", _unreadable)

    assert reminder_daemon._retained_reply_ids(7, frozenset({40})) == set()
