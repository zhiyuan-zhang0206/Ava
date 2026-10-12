"""Persisted one-attempt schedule admission and outage alerts survive restart."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from psycopg_pool import ConnectionPool

import services.wake.schedule_manager.manager as sm
from base.cluster import session_name
from base.config import settings
from base.deploy.lifecycle import start_serving


class _FakeBackend:
    def __init__(self) -> None:
        self.live: list[str] = []

    def list_sessions(self, prefix: str = "") -> list[str]:
        return [s for s in self.live if s.startswith(prefix)]

    def has_session(self, name: str) -> bool:
        return name in self.live

    def session_generation(self, name: str) -> str | None:
        return None

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
        launched.append(int(str(env["AVA_SCHEDULE_ID"])))
        backend.live.append(name)  # launch => session appears
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
    """(launch_count, backing off) of a schedule row."""
    row = conn.execute(
        "SELECT launch_count, next_launch_at > clock_timestamp() FROM schedules WHERE id = %s",
        (sid,),
    ).fetchone()
    assert row is not None
    return row[0], bool(row[1])


def _end_backoff(conn: psycopg.Connection, sid: int) -> None:
    """Move the row's backoff deadline into the past."""
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
    """Make the current outage `seconds` old."""
    conn.execute(
        "UPDATE schedules SET not_live_since = clock_timestamp() - make_interval(secs => %s) "
        "WHERE id = %s",
        (seconds, sid),
    )
    conn.commit()


def test_error_schedule_not_relaunched_after_restart(
    db_conn: psycopg.Connection, pool: ConnectionPool, fake_session: tuple[_FakeBackend, list[int]]
) -> None:
    # The terminal failure lives in the DB (status='error'). A fresh ScheduleManager
    # (a restarted service) must treat an enabled+error+no-session row as
    # terminal, not a vanished session to relaunch — the crashloop-amnesia bug.
    _backend, launched = fake_session
    sid = _insert(db_conn, "err-a")
    _set_status(db_conn, sid, "error")

    sm.ScheduleManager(pool).reconcile()

    assert launched == []  # not resurrected with a clean counter
    assert _status(db_conn, sid) == "error"


def test_missing_error_schedule_alerts_after_two_hours_and_rearms_after_recovery(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    fake_session: tuple[_FakeBackend, list[int]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    backend, _launched = fake_session
    sid = _insert(db_conn, "stalled-error")
    _set_status(db_conn, sid, "error")
    emitted: list[tuple[str, dict[str, object]]] = []

    def capture_emit(_category: object, event_name: str, **kwargs: Any) -> None:
        emitted.append((event_name, cast(dict[str, object], kwargs["attributes"])))

    monkeypatch.setattr(sm.telemetry, "emit", capture_emit)
    manager = sm.ScheduleManager(pool)

    with caplog.at_level(logging.WARNING, logger=sm.__name__):
        manager.reconcile()  # first sessionless observation starts the clock
        _age_not_live(db_conn, sid, sm._STALL_ALERT_AFTER_S - 5)
        manager.reconcile()
        assert emitted == []
        _age_not_live(db_conn, sid, sm._STALL_ALERT_AFTER_S + 0.1)
        manager.reconcile()
        manager.reconcile()  # one alert per outage

        name = session_name(f"schedule-{sid}")
        backend.live.append(name)
        manager.reconcile()  # seen live again: the alert rearms
        backend.live.remove(name)
        manager.reconcile()
        _age_not_live(db_conn, sid, sm._STALL_ALERT_AFTER_S + 0.1)
        manager.reconcile()

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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restarted service neither restarts the two-hour clock nor alerts again for
    an outage it already alerted on: both live on the row."""
    _backend, _launched = fake_session
    sid = _insert(db_conn, "stall-restart")
    _set_status(db_conn, sid, "error")
    emitted: list[str] = []

    def capture_emit(_category: object, event_name: str, **_kwargs: Any) -> None:
        emitted.append(event_name)

    monkeypatch.setattr(sm.telemetry, "emit", capture_emit)

    sm.ScheduleManager(pool).reconcile()
    _age_not_live(db_conn, sid, sm._STALL_ALERT_AFTER_S + 1)
    sm.ScheduleManager(pool).reconcile()  # a fresh process meets an aged outage
    assert emitted == ["schedule_stalled"]
    sm.ScheduleManager(pool).reconcile()  # and another fresh process stays quiet
    assert emitted == ["schedule_stalled"]


def test_completed_and_disabled_schedules_do_not_stall_alert(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    fake_session: tuple[_FakeBackend, list[int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed = _insert(db_conn, "completed-no-alert")
    _set_status(db_conn, completed, "completed")
    disabled = _insert(db_conn, "disabled-no-alert", enabled=False)
    for sid in (completed, disabled):
        _age_not_live(db_conn, sid, sm._STALL_ALERT_AFTER_S + 1)  # a leftover outage
    emitted: list[str] = []

    def capture_emit(_category: object, event_name: str, **_kwargs: Any) -> None:
        emitted.append(event_name)

    monkeypatch.setattr(sm.telemetry, "emit", capture_emit)

    sm.ScheduleManager(pool).reconcile()

    assert emitted == []
    cleared = db_conn.execute(
        "SELECT count(*) FROM schedules WHERE not_live_since IS NULL AND stall_alerted_at IS NULL "
        "AND id = ANY(%s)",
        ([completed, disabled],),
    ).fetchone()
    assert cleared == (2,)


def test_a_launch_is_claimed_once(
    db_conn: psycopg.Connection, pool: ConnectionPool, fake_session: tuple[_FakeBackend, list[int]]
) -> None:
    """The claim holds only for the count the reconcile read and outside a
    backoff, so two callers cannot both launch the same attempt."""
    _backend, _launched = fake_session
    sid = _insert(db_conn, "claim-once")
    mgr = sm.ScheduleManager(pool)

    assert mgr._claim_launch(sid, 0) is True
    assert mgr._claim_launch(sid, 0) is False
    _end_backoff(db_conn, sid)
    assert mgr._claim_launch(sid, 0) is False
    assert sm.ScheduleManager(pool)._claim_launch(sid, 0) is False
    assert _backoff_state(db_conn, sid) == (1, False)
