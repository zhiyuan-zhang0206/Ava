from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import psycopg
import pytest
from psycopg_pool import ConnectionPool

import services.wake.schedule_manager.manager as sm
from base.cluster import session_name
from base.config import settings
from base.deploy.lifecycle import start_serving
from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events


class _FakeBackend:
    def __init__(self) -> None:
        self.live: list[str] = []
        self.created: list[str] = []
        self.epoch: str | None = "host-a"

    def list_sessions(self, prefix: str = "") -> list[str]:
        return [s for s in self.live if s.startswith(prefix)]

    def has_session(self, name: str) -> bool:
        return name in self.live

    def session_generation(self, name: str) -> str | None:
        return None

    def host_generation(self) -> str | None:
        return self.epoch

    def new_session(self, name: str, cmd: str, cwd: object, *, env: dict[str, object]) -> bool:
        self.live.append(name)
        return True

    def kill_session(self, name: str, **_kw: object) -> tuple[bool, str]:
        self.live = [s for s in self.live if s != name]
        return True, "forced"


@pytest.fixture
def pool(db_url: str) -> Iterator[ConnectionPool[psycopg.Connection]]:
    p: ConnectionPool[psycopg.Connection] = ConnectionPool(
        settings.data_plane.db_url, min_size=1, max_size=2, open=True
    )
    try:
        yield p
    finally:
        p.close()


@pytest.fixture
def fake_session(
    serving_root: start_serving.RootBirth, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[_FakeBackend, list[int]]:
    from base.sessions.pty import allocation_freeze

    backend = _FakeBackend()
    launched: list[int] = []

    def _fake_new_session(
        _self: object, name: str, cmd: str, cwd: object, *, env: dict[str, object]
    ) -> bool:
        launched.append(int(str(env["MEIVA_SCHEDULE_ID"])))
        backend.created.append(name)
        backend.live.append(name)
        return True

    monkeypatch.setattr(sm, "get_shell_backend", lambda: backend)
    monkeypatch.setattr(_FakeBackend, "new_session", _fake_new_session)
    monkeypatch.setattr(allocation_freeze, "current_generation", lambda: None)
    monkeypatch.setattr(start_serving, "state_path", lambda: tmp_path / "start-serving.json")
    generation = start_serving.begin_start()
    assert start_serving.mark_serving(generation, runtime=serving_root.runtime) is True
    return backend, launched


def _insert(conn: psycopg.Connection, name: str, *, enabled: bool = True) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO schedules (name, script, command, enabled, status) "
            "VALUES (%s, 'x', 'python schedule.py', %s, 'stopped') RETURNING id",
            (name, enabled),
        )
        row = cur.fetchone()
    conn.commit()
    assert row is not None
    return row[0]


def _status(conn: psycopg.Connection, sid: int) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM schedules WHERE id = %s", (sid,))
        row = cur.fetchone()
    assert row is not None
    return row[0]


def _set_status(conn: psycopg.Connection, sid: int, status: str) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE schedules SET status = %s WHERE id = %s", (status, sid))
    conn.commit()


def _backoff_state(conn: psycopg.Connection, sid: int) -> tuple[int, bool]:
    row = conn.execute(
        "SELECT launch_count, next_launch_at > clock_timestamp() FROM schedules WHERE id = %s",
        (sid,),
    ).fetchone()
    assert row is not None
    return row[0], bool(row[1])


def _end_backoff(conn: psycopg.Connection, sid: int) -> None:
    conn.execute(
        "UPDATE schedules SET next_launch_at = clock_timestamp() - interval '1 second' "
        "WHERE id = %s",
        (sid,),
    )
    conn.commit()


def _set_backoff(conn: psycopg.Connection, sid: int, count: int, *, seconds_ahead: float) -> None:
    conn.execute(
        "UPDATE schedules SET launch_count = %s, "
        "next_launch_at = clock_timestamp() + make_interval(secs => %s) WHERE id = %s",
        (count, seconds_ahead, sid),
    )
    conn.commit()


def _age_not_live(conn: psycopg.Connection, sid: int, seconds: float) -> None:
    conn.execute(
        "UPDATE schedules SET not_live_since = clock_timestamp() - make_interval(secs => %s) "
        "WHERE id = %s",
        (seconds, sid),
    )
    conn.commit()


def test_error_schedule_not_relaunched_after_restart(
    db_conn: psycopg.Connection, pool: ConnectionPool, fake_session: tuple[_FakeBackend, list[int]]
) -> None:
    _backend, launched = fake_session
    sid = _insert(db_conn, "err-a")
    _set_status(db_conn, sid, "error")

    sm.ScheduleManager(pool).reconcile()

    assert launched == []
    assert _status(db_conn, sid) == "error"


def test_missing_error_schedule_alerts_after_two_hours_and_rearms_after_recovery(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    fake_session: tuple[_FakeBackend, list[int]],
    caplog: pytest.LogCaptureFixture,
    emitted_events: EmittedEvents,
) -> None:
    backend, _launched = fake_session
    sid = _insert(db_conn, "stalled-error")
    _set_status(db_conn, sid, "error")
    manager = sm.ScheduleManager(pool)

    with caplog.at_level(logging.WARNING, logger=sm.__name__):
        manager.reconcile()
        _age_not_live(db_conn, sid, sm.STALL_ALERT_AFTER_S - 5)
        manager.reconcile()
        assert len(emitted_events) == 0
        _age_not_live(db_conn, sid, sm.STALL_ALERT_AFTER_S + 0.1)
        manager.reconcile()
        manager.reconcile()

        name = session_name(f"schedule-{sid}")
        backend.live.append(name)
        manager.reconcile()
        backend.live.remove(name)
        manager.reconcile()
        _age_not_live(db_conn, sid, sm.STALL_ALERT_AFTER_S + 0.1)
        manager.reconcile()

    emitted = [(event.event_name, event.attributes) for event in emitted_events]
    assert [(name, attrs["schedule_id"], attrs["status"]) for name, attrs in emitted] == [
        ("schedule_stalled", sid, "error"),
        ("schedule_stalled", sid, "error"),
    ]
    assert all(7200.1 <= cast(float, attrs["stalled_seconds"]) < 7260 for _name, attrs in emitted)
    assert caplog.text.count(f"schedule {sid} has had no live session") == 2


def test_the_stall_alert_clock_and_flag_survive_a_restart(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    fake_session: tuple[_FakeBackend, list[int]],
    emitted_events: EmittedEvents,
) -> None:
    _backend, _launched = fake_session
    sid = _insert(db_conn, "stall-restart")
    _set_status(db_conn, sid, "error")

    sm.ScheduleManager(pool).reconcile()
    _age_not_live(db_conn, sid, sm.STALL_ALERT_AFTER_S + 1)
    sm.ScheduleManager(pool).reconcile()
    assert emitted_events.names() == ["schedule_stalled"]
    sm.ScheduleManager(pool).reconcile()
    assert emitted_events.names() == ["schedule_stalled"]


def test_completed_and_disabled_schedules_do_not_stall_alert(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    fake_session: tuple[_FakeBackend, list[int]],
    emitted_events: EmittedEvents,
) -> None:
    completed = _insert(db_conn, "completed-no-alert")
    _set_status(db_conn, completed, "completed")
    disabled = _insert(db_conn, "disabled-no-alert", enabled=False)
    for sid in (completed, disabled):
        _age_not_live(db_conn, sid, sm.STALL_ALERT_AFTER_S + 1)

    sm.ScheduleManager(pool).reconcile()

    assert len(emitted_events) == 0
    cleared = db_conn.execute(
        "SELECT count(*) FROM schedules WHERE not_live_since IS NULL AND stall_alerted_at IS NULL "
        "AND id = ANY(%s)",
        ([completed, disabled],),
    ).fetchone()
    assert cleared == (2,)


def test_a_launch_is_claimed_once(
    db_conn: psycopg.Connection, pool: ConnectionPool, fake_session: tuple[_FakeBackend, list[int]]
) -> None:
    _backend, _launched = fake_session
    sid = _insert(db_conn, "claim-once")
    mgr = sm.ScheduleManager(pool)

    assert mgr._claim_launch(sid, 0, "host-a") is True
    assert mgr._claim_launch(sid, 0, "host-a") is False
    _end_backoff(db_conn, sid)
    assert mgr._claim_launch(sid, 0, "host-a") is False
    assert sm.ScheduleManager(pool)._claim_launch(sid, 0, "host-a") is False
    assert _backoff_state(db_conn, sid) == (1, False)


def _insert_running(conn: psycopg.Connection, name: str, *, generation: str | None) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO schedules (name, script, command, enabled, status, launch_count, "
            "launch_generation) VALUES (%s, 'x', 'python schedule.py', true, 'running', 1, %s) "
            "RETURNING id",
            (name, generation),
        )
        row = cur.fetchone()
    conn.commit()
    assert row is not None
    return row[0]


def _state(conn: psycopg.Connection, sid: int) -> tuple[str, str | None, int]:
    row = conn.execute(
        "SELECT status, launch_generation, launch_count FROM schedules WHERE id = %s", (sid,)
    ).fetchone()
    assert row is not None
    return row[0], row[1], row[2]


def _attributes(events: EmittedEvents) -> list[dict[str, object]]:
    return [dict(event.attributes) for event in events]


def test_a_replaced_session_host_relaunches_the_schedule_once(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    fake_session: tuple[_FakeBackend, list[int]],
    emitted_events: EmittedEvents,
) -> None:
    backend, _launched = fake_session
    sid = _insert_running(db_conn, "host-wave", generation="host-a")
    backend.epoch = "host-b"

    sm.ScheduleManager(pool).reconcile()

    assert _state(db_conn, sid) == ("running", "host-b", 1)
    assert backend.created == [session_name(f"schedule-{sid}")]
    assert emitted_events.names() == ["schedule_auto_restart"]
    assert _attributes(emitted_events) == [
        {
            "schedule_id": sid,
            "previous_generation": "host-a",
            "current_generation": "host-b",
            "restarts_in_window": 1,
        }
    ]


def test_a_second_silent_death_in_the_same_generation_is_terminal(
    db_conn: psycopg.Connection, pool: ConnectionPool, fake_session: tuple[_FakeBackend, list[int]]
) -> None:
    backend, _launched = fake_session
    sid = _insert_running(db_conn, "host-wave-then-dies", generation="host-a")
    manager = sm.ScheduleManager(pool)
    backend.epoch = "host-b"
    manager.reconcile()
    assert _state(db_conn, sid)[0] == "running"

    backend.live.clear()
    manager.reconcile()

    assert _state(db_conn, sid)[:2] == ("error", "host-b")
    assert len(backend.created) == 1


def test_an_unreachable_session_host_marks_nothing_lost(
    db_conn: psycopg.Connection, pool: ConnectionPool, fake_session: tuple[_FakeBackend, list[int]]
) -> None:
    backend, _launched = fake_session
    sid = _insert_running(db_conn, "host-absent", generation="host-a")
    backend.epoch = None

    sm.ScheduleManager(pool).reconcile()

    assert _state(db_conn, sid) == ("running", "host-a", 1)
    assert backend.created == []
    still_clocked = db_conn.execute(
        "SELECT not_live_since IS NULL FROM schedules WHERE id = %s", (sid,)
    ).fetchone()
    assert still_clocked == (True,)


def test_an_attempt_from_before_the_column_is_not_relaunched(
    db_conn: psycopg.Connection, pool: ConnectionPool, fake_session: tuple[_FakeBackend, list[int]]
) -> None:
    backend, _launched = fake_session
    sid = _insert_running(db_conn, "host-unknown-attempt", generation=None)
    backend.epoch = "host-b"

    sm.ScheduleManager(pool).reconcile()

    assert _state(db_conn, sid)[0] == "error"
    assert backend.created == []


def test_the_window_cap_stops_relaunching_a_host_that_keeps_restarting(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    fake_session: tuple[_FakeBackend, list[int]],
    emitted_events: EmittedEvents,
) -> None:
    backend, _launched = fake_session
    sid = _insert_running(db_conn, "host-storm", generation="gen-0")
    manager = sm.ScheduleManager(pool)

    for index in range(sm.auto_restart_max_per_window()):
        backend.epoch = f"gen-{index + 1}"
        manager.reconcile()
        assert _state(db_conn, sid)[0] == "running"
        backend.live.clear()

    backend.epoch = "gen-final"
    manager.reconcile()

    assert _state(db_conn, sid)[0] == "error"
    assert len(backend.created) == sm.auto_restart_max_per_window()
    assert emitted_events.names() == [
        *(["schedule_auto_restart"] * sm.auto_restart_max_per_window()),
        "schedule_auto_restart_suppressed",
    ]
    suppressed = _attributes(emitted_events)[-1]
    assert suppressed["previous_generation"] == f"gen-{sm.auto_restart_max_per_window()}"
    assert suppressed["current_generation"] == "gen-final"
    assert suppressed["restarts_in_window"] == sm.auto_restart_max_per_window()


def test_a_refused_launch_does_not_consume_the_auto_restart_budget(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    fake_session: tuple[_FakeBackend, list[int]],
    emitted_events: EmittedEvents,
) -> None:
    backend, _launched = fake_session
    sid = _insert_running(db_conn, "host-gated", generation="host-a")
    backend.epoch = "host-b"
    manager = sm.ScheduleManager(pool)
    start_serving.begin_start()

    for _ in range(sm.auto_restart_max_per_window() + 2):
        manager.reconcile()

    assert _state(db_conn, sid)[0] == "running"
    assert backend.created == []
    assert "schedule_auto_restart" not in emitted_events.names()
    assert "schedule_auto_restart_suppressed" not in emitted_events.names()
