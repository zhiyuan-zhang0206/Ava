"""Tests for gateway/schedule_manager.py — the reconcile loop's decisions.

The DB is real (a small ConnectionPool on the test DB); the session backend is
faked (a class-level `new_session` stub + `get_shell_backend` monkeypatch), so
these tests assert the manager's launch / adopt / kill / backoff / breaker logic
without a real session backend.
"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest
from psycopg_pool import ConnectionPool

import gateway.schedule_manager as sm
from shared.cluster import session_name
from shared.config import settings


class _FakeBackend:
    def __init__(self) -> None:
        self.live: list[str] = []
        self.killed: list[str] = []

    def list_sessions(self, prefix: str = "") -> list[str]:
        return [s for s in self.live if s.startswith(prefix)]

    def has_session(self, name: str) -> bool:
        return name in self.live

    def new_session(self, name: str, cmd: str, cwd: object, *, env: dict[str, object]) -> bool:
        return True

    def capture_pane(self, name: str, lines: int = 200, *, scrollback: bool = True) -> str:
        return f"output of {name}"

    def kill_session(self, name: str, **_kw: object) -> tuple[bool, str]:
        self.killed.append(name)
        self.live = [s for s in self.live if s != name]
        return True, ""


@pytest.fixture
def pool() -> Iterator[ConnectionPool[psycopg.Connection]]:
    p: ConnectionPool[psycopg.Connection] = ConnectionPool(
        settings.data_plane.db_url, min_size=1, max_size=2, open=True
    )
    try:
        yield p
    finally:
        p.close()


@pytest.fixture
def fake_session(monkeypatch: pytest.MonkeyPatch) -> tuple[_FakeBackend, list[int]]:
    backend = _FakeBackend()
    launched: list[int] = []

    def _fake_new_session(
        _self: object, name: str, cmd: str, cwd: object, *, env: dict[str, object]
    ) -> bool:
        # The schedule id rides env (AVA_SCHEDULE_ID) — the cmd tail is now
        # `... schedule_runner <id>; exit $?`, so argv parsing is a trap.
        sid = int(str(env["AVA_SCHEDULE_ID"]))
        launched.append(sid)
        backend.live.append(session_name(f"schedule-{sid}"))  # launch => session appears
        return True

    monkeypatch.setattr(sm, "get_shell_backend", lambda: backend)
    monkeypatch.setattr(_FakeBackend, "new_session", _fake_new_session)
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


def test_launches_enabled_missing_session(
    db_conn: psycopg.Connection, pool: ConnectionPool, fake_session: tuple[_FakeBackend, list[int]]
) -> None:
    _backend, launched = fake_session
    sid = _insert(db_conn, "job-a")

    sm.ScheduleManager(pool)._reconcile()

    assert launched == [sid]
    assert _status(db_conn, sid) == "running"


def test_adopts_live_session_without_relaunch(
    db_conn: psycopg.Connection, pool: ConnectionPool, fake_session: tuple[_FakeBackend, list[int]]
) -> None:
    backend, launched = fake_session
    sid = _insert(db_conn, "job-b")
    backend.live.append(session_name(f"schedule-{sid}"))  # already running (survived a restart)

    sm.ScheduleManager(pool)._reconcile()

    assert launched == []  # adopted, not relaunched


def test_reaps_stale_session_before_launch(
    db_conn: psycopg.Connection, pool: ConnectionPool, fake_session: tuple[_FakeBackend, list[int]]
) -> None:
    """A same-name session surviving a gateway restart must be reaped, not
    collide with the spawn ("duplicate session" -> 5x breaker trip ->
    every schedule paused; #1119)."""
    backend, launched = fake_session
    sid = _insert(db_conn, "job-stale")
    stale = session_name(f"schedule-{sid}")
    backend.live.append(stale)  # leftover from the pre-restart gateway run

    sm.ScheduleManager(pool)._launch(sid)

    assert stale in backend.killed
    assert launched == [sid]
    assert _status(db_conn, sid) == "running"


def test_kills_orphan_session(
    db_conn: psycopg.Connection, pool: ConnectionPool, fake_session: tuple[_FakeBackend, list[int]]
) -> None:
    backend, _launched = fake_session
    sid = _insert(db_conn, "job-c", enabled=False)  # disabled => not desired
    orphan = session_name(f"schedule-{sid}")
    backend.live.append(orphan)

    sm.ScheduleManager(pool)._reconcile()

    assert orphan in backend.killed
    assert _status(db_conn, sid) == "stopped"


def test_backoff_skips_relaunch_within_window(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    fake_session: tuple[_FakeBackend, list[int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, launched = fake_session
    sid = _insert(db_conn, "job-d")
    clock = {"t": 1000.0}
    monkeypatch.setattr(sm.time, "monotonic", lambda: clock["t"])
    mgr = sm.ScheduleManager(pool)

    mgr._reconcile()  # launch #1 (count 0 -> deadline t+2s)
    assert launched == [sid]
    backend.live.clear()  # it crashed immediately
    mgr._reconcile()  # still within the 2s backoff window -> no relaunch
    assert launched == [sid]
    clock["t"] += 3.0  # past the window
    mgr._reconcile()  # now relaunch #2
    assert launched == [sid, sid]


def test_breaker_trips_after_max_launches(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    fake_session: tuple[_FakeBackend, list[int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, launched = fake_session
    sid = _insert(db_conn, "job-e")
    clock = {"t": 0.0}
    monkeypatch.setattr(sm.time, "monotonic", lambda: clock["t"])
    mgr = sm.ScheduleManager(pool)

    # Each round: reconcile launches, the schedule "crashes" (session vanishes),
    # advance time past the growing backoff. After _BREAKER_MAX launches it trips.
    for _ in range(sm._BREAKER_MAX + 3):
        mgr._reconcile()
        backend.live.clear()
        clock["t"] += sm._BACKOFF_CAP_S + 1

    assert len(launched) == sm._BREAKER_MAX  # stopped launching at the ceiling
    assert _status(db_conn, sid) == "error"


def _set_status(conn: psycopg.Connection, sid: int, status: str) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE schedules SET status = %s WHERE id = %s", (status, sid))
    conn.commit()


def test_completed_schedule_not_relaunched(
    db_conn: psycopg.Connection, pool: ConnectionPool, fake_session: tuple[_FakeBackend, list[int]]
) -> None:
    # A clean exit leaves the schedule enabled but status='completed' with no live
    # session. Reconcile must treat that as terminal, not a vanished session to
    # relaunch (the false-crash bug this fixes).
    _backend, launched = fake_session
    sid = _insert(db_conn, "done-a")
    _set_status(db_conn, sid, "completed")

    sm.ScheduleManager(pool)._reconcile()

    assert launched == []  # left alone, not resurrected
    assert _status(db_conn, sid) == "completed"


def test_completed_clears_prior_crash_backoff(
    db_conn: psycopg.Connection, pool: ConnectionPool, fake_session: tuple[_FakeBackend, list[int]]
) -> None:
    _backend, launched = fake_session
    sid = _insert(db_conn, "done-b")
    _set_status(db_conn, sid, "completed")
    mgr = sm.ScheduleManager(pool)
    mgr._backoff[sid] = (3, 9e9)  # a couple crashes before it finally completed

    mgr._reconcile()

    assert launched == []
    assert sid not in mgr._backoff  # a clean finish wipes the crash backoff


def test_error_schedule_not_relaunched_after_restart(
    db_conn: psycopg.Connection, pool: ConnectionPool, fake_session: tuple[_FakeBackend, list[int]]
) -> None:
    # The breaker trip lives in the DB (status='error'), not the in-memory
    # backoff. A fresh ScheduleManager (empty _backoff, as after a gateway
    # restart) must treat an enabled+error+no-session row as terminal, not a
    # vanished session to relaunch with a fresh counter — the crashloop-amnesia
    # bug this fixes.
    _backend, launched = fake_session
    sid = _insert(db_conn, "err-a")
    _set_status(db_conn, sid, "error")

    mgr = sm.ScheduleManager(pool)  # fresh: _backoff is empty
    assert sid not in mgr._backoff
    mgr._reconcile()

    assert launched == []  # not resurrected with a clean counter
    assert _status(db_conn, sid) == "error"


def test_sync_recovers_errored_schedule(
    db_conn: psycopg.Connection, pool: ConnectionPool, fake_session: tuple[_FakeBackend, list[int]]
) -> None:
    # The recovery path: an explicit start/restart routes through sync(), which
    # relaunches and pulls the row out of 'error' back to 'running'.
    backend, launched = fake_session
    sid = _insert(db_conn, "err-b")
    _set_status(db_conn, sid, "error")

    sm.ScheduleManager(pool)._sync_blocking(sid)

    assert launched == [sid]
    assert session_name(f"schedule-{sid}") in backend.live
    assert _status(db_conn, sid) == "running"


def test_sync_launches_enabled_and_kills_disabled(
    db_conn: psycopg.Connection, pool: ConnectionPool, fake_session: tuple[_FakeBackend, list[int]]
) -> None:
    backend, launched = fake_session
    sid = _insert(db_conn, "sync-a", enabled=True)
    mgr = sm.ScheduleManager(pool)

    mgr._sync_blocking(sid)  # enabled -> launch
    assert launched == [sid]
    assert session_name(f"schedule-{sid}") in backend.live

    with db_conn.cursor() as cur:  # now disable and sync -> kill
        cur.execute("UPDATE schedules SET enabled = false WHERE id = %s", (sid,))
    db_conn.commit()
    mgr._sync_blocking(sid)
    assert session_name(f"schedule-{sid}") in backend.killed
    assert launched == [sid]  # not relaunched


def test_sync_clears_backoff(
    db_conn: psycopg.Connection, pool: ConnectionPool, fake_session: tuple[_FakeBackend, list[int]]
) -> None:
    _backend, _launched = fake_session
    sid = _insert(db_conn, "sync-b")
    mgr = sm.ScheduleManager(pool)
    mgr._backoff[sid] = (sm._BREAKER_MAX, 9e9)  # pretend it tripped
    mgr._sync_blocking(sid)
    assert sid not in mgr._backoff  # a deliberate sync resets the crash backoff


def test_capture_returns_none_without_session(
    db_conn: psycopg.Connection, pool: ConnectionPool, fake_session: tuple[_FakeBackend, list[int]]
) -> None:
    _backend, _launched = fake_session
    mgr = sm.ScheduleManager(pool)
    assert mgr._capture_blocking(123, 200) is None


def test_schedule_manager_uses_shell_backend() -> None:
    """The manager's sessions are PTY-supervisor sessions — it must use
    `get_shell_backend()` (PtySessionBackend on POSIX). The schedule migration
    (S6, 2026-08-09) moved schedule sessions into the PTY
    supervisor, the same backend as agent shells/watchers: one supervisor, one
    record namespace, no raw spawn on the schedule path. The service backend
    (S7 — services AND orchestration) must NOT see schedules — a reconcile
    loop on it would collide with the live PTY sessions on relaunch (the
    inverse of #1119).
    """
    from shared.session_backend import get_backend, get_shell_backend

    assert sm.get_shell_backend is get_shell_backend
    assert sm.get_shell_backend is not get_backend


def test_successful_launch_clears_stale_last_error(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    fake_session: tuple[_FakeBackend, list[int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (2026-08-11): a schedule recovered via explicit restart kept
    the breaker's "auto-restart paused" text forever -- _launch set status to
    'running' but never nulled last_error. A successful launch must clear it."""
    _backend, launched = fake_session
    sid = _insert(db_conn, "job-recovered")
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE schedules SET status = 'error', "
            "last_error = 'auto-restart paused: schedule crashed 5 times without staying up' "
            "WHERE id = %s",
            (sid,),
        )
    db_conn.commit()
    # Simulate an explicit restart: clear status to a launchable state first
    # (the manager's API restart does this), then reconcile launches it.
    _set_status(db_conn, sid, "stopped")

    clock = {"t": 0.0}
    monkeypatch.setattr(sm.time, "monotonic", lambda: clock["t"])
    mgr = sm.ScheduleManager(pool)
    mgr._reconcile()
    assert launched == [sid]
    with db_conn.cursor() as cur:
        cur.execute("SELECT status, last_error FROM schedules WHERE id = %s", (sid,))
        row = cur.fetchone()
    assert row is not None
    status, last_error = row
    assert status == "running"
    assert last_error is None, "stale breaker text must be cleared on successful launch"
