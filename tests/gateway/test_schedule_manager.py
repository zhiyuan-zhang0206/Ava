"""Tests for gateway/schedule_manager.py — the reconcile loop's decisions.

The DB is real (a small ConnectionPool on the test DB); the session backend is
faked (a class-level `new_session` stub + `get_shell_backend` monkeypatch), so
these tests assert the manager's launch / adopt / kill / backoff / breaker logic
without a real session backend.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

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
        self.kill_results: list[bool] = []
        self.launch_envs: list[dict[str, object]] = []
        self.generations: dict[str, str | None] = {}

    def list_sessions(self, prefix: str = "") -> list[str]:
        return [s for s in self.live if s.startswith(prefix)]

    def has_session(self, name: str) -> bool:
        return name in self.live

    def session_generation(self, name: str) -> str | None:
        return self.generations.get(name)

    def new_session(self, name: str, cmd: str, cwd: object, *, env: dict[str, object]) -> bool:
        return True

    def capture_pane(self, name: str, lines: int = 200, *, scrollback: bool = True) -> str:
        return f"output of {name}"

    def kill_session(self, name: str, **_kw: object) -> tuple[bool, str]:
        self.killed.append(name)
        reaped = self.kill_results.pop(0) if self.kill_results else True
        if reaped:
            self.live = [s for s in self.live if s != name]
        return reaped, "forced" if reaped else "survived"


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
def fake_session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[_FakeBackend, list[int]]:
    from shared import start_serving
    from shared.pty_sessions import allocation_freeze

    backend = _FakeBackend()
    launched: list[int] = []

    def _fake_new_session(
        _self: object, name: str, cmd: str, cwd: object, *, env: dict[str, object]
    ) -> bool:
        # The schedule id rides env (AVA_SCHEDULE_ID) — the cmd tail is now
        # `... schedule_runner <id>; exit $?`, so argv parsing is a trap.
        sid = int(str(env["AVA_SCHEDULE_ID"]))
        launched.append(sid)
        backend.launch_envs.append(dict(env))
        backend.live.append(session_name(f"schedule-{sid}"))  # launch => session appears
        return True

    monkeypatch.setattr(sm, "get_shell_backend", lambda: backend)
    monkeypatch.setattr(_FakeBackend, "new_session", _fake_new_session)
    monkeypatch.setattr(allocation_freeze, "current_generation", lambda: None)
    monkeypatch.setattr(start_serving, "state_path", lambda: tmp_path / "start-serving.json")
    generation = start_serving.begin_start()
    assert start_serving.mark_serving(generation) is True
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


def _insert_null_run(conn: psycopg.Connection, sid: int) -> int:
    """A run-history row left in-progress (ok IS NULL) by a process."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO schedule_runs (schedule_id) VALUES (%s) RETURNING id",
            (sid,),
        )
        row = cur.fetchone()
    conn.commit()
    assert row is not None
    return row[0]


def _run_row(conn: psycopg.Connection, rid: int) -> tuple[bool | None, str | None]:
    with conn.cursor() as cur:
        cur.execute("SELECT ok, note FROM schedule_runs WHERE id = %s", (rid,))
        row = cur.fetchone()
    assert row is not None
    return row[0], row[1]


def test_reconcile_closes_orphan_null_rows(
    db_conn: psycopg.Connection, pool: ConnectionPool, fake_session: tuple[_FakeBackend, list[int]]
) -> None:
    # QA P2-2: a run row left in-progress by a dead process (SIGKILLed stop /
    # external kill / gateway-restart reap) must be closed, or the run drawer
    # shows a permanent in-progress "…". A disabled schedule with no session
    # and a NULL row is swept on the next reconcile.
    _backend, launched = fake_session
    sid = _insert(db_conn, "orphan-a", enabled=False)
    rid = _insert_null_run(db_conn, sid)

    sm.ScheduleManager(pool)._reconcile()

    assert launched == []
    assert _run_row(db_conn, rid) == (False, "interrupted")


def test_reconcile_keeps_null_row_for_live_session(
    db_conn: psycopg.Connection, pool: ConnectionPool, fake_session: tuple[_FakeBackend, list[int]]
) -> None:
    # A NULL row for a schedule WITH a live session is a genuinely running
    # process — the sweep must not touch it (a resident schedule's row stays
    # in-progress for its whole lifetime).
    backend, launched = fake_session
    sid = _insert(db_conn, "orphan-b")
    rid = _insert_null_run(db_conn, sid)
    backend.live.append(session_name(f"schedule-{sid}"))

    sm.ScheduleManager(pool)._reconcile()

    assert launched == []
    assert _run_row(db_conn, rid) == (None, None)


def test_launch_closes_null_rows_of_dead_predecessor(
    db_conn: psycopg.Connection, pool: ConnectionPool, fake_session: tuple[_FakeBackend, list[int]]
) -> None:
    # Restart/edit-save shape: an enabled schedule whose old process was
    # SIGKILLed (NULL row, no live session) is relaunched — the old row must
    # close so the drawer does not show it beside the new run's row.
    _backend, launched = fake_session
    sid = _insert(db_conn, "orphan-c")
    rid = _insert_null_run(db_conn, sid)

    sm.ScheduleManager(pool)._reconcile()

    assert launched == [sid]
    assert _run_row(db_conn, rid) == (False, "interrupted")


def test_sync_stop_closes_orphan_rows_immediately(
    db_conn: psycopg.Connection, pool: ConnectionPool, fake_session: tuple[_FakeBackend, list[int]]
) -> None:
    # The explicit stop path (API stop) kills the session directly — the
    # killed process's in-progress row closes right away, not at the next
    # reconcile tick.
    backend, launched = fake_session
    sid = _insert(db_conn, "orphan-d", enabled=True)
    rid = _insert_null_run(db_conn, sid)
    backend.live.append(session_name(f"schedule-{sid}"))
    with db_conn.cursor() as cur:
        cur.execute("UPDATE schedules SET enabled = false WHERE id = %s", (sid,))
    db_conn.commit()

    sm.ScheduleManager(pool)._sync_blocking(sid)

    assert session_name(f"schedule-{sid}") in backend.killed
    assert launched == []  # disabled — not relaunched
    assert _run_row(db_conn, rid) == (False, "interrupted")


def test_launches_enabled_missing_session(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    pool: ConnectionPool,
    fake_session: tuple[_FakeBackend, list[int]],
) -> None:
    """An enabled schedule remains desired after a current-generation restart."""
    from shared.pty_sessions import allocation_freeze

    _backend, launched = fake_session
    monkeypatch.setattr(allocation_freeze, "current_generation", lambda: "current-generation")
    sid = _insert(db_conn, "job-a")

    sm.ScheduleManager(pool)._reconcile()

    assert launched == [sid]
    assert _status(db_conn, sid) == "running"


def test_reconcile_defers_enabled_launch_until_the_host_is_serving(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    fake_session: tuple[_FakeBackend, list[int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed gateway start leaves an enabled schedule stopped until retry.

    Removing the serving check would launch on the first reconcile; applying it
    to the whole reconcile would break disabled-schedule cleanup tests.
    """
    from shared import start_serving

    _backend, launched = fake_session
    start_serving.clear_serving()
    sid = _insert(db_conn, "await-serving")
    manager = sm.ScheduleManager(pool)

    manager._reconcile()

    assert launched == []
    assert _status(db_conn, sid) == "stopped"

    generation = start_serving.begin_start()
    assert start_serving.mark_serving(generation) is True
    manager._reconcile()

    assert launched == [sid]
    assert _status(db_conn, sid) == "running"


def test_live_superseded_generation_is_reaped_before_schedule_reconcile(
    monkeypatch: pytest.MonkeyPatch,
    pool: ConnectionPool,
    fake_session: tuple[_FakeBackend, list[int]],
) -> None:
    """A same-name schedule session from a flip cannot be adopted as current."""
    from shared.pty_sessions import allocation_freeze

    backend, _launched = fake_session
    name = session_name("schedule-77")
    backend.live.append(name)
    backend.generations[name] = "previous-generation"
    monkeypatch.setattr(allocation_freeze, "current_generation", lambda: "current-generation")

    assert sm.ScheduleManager(pool)._live_ids() == set()
    assert backend.killed == [name]


def test_superseded_generation_reap_retries_before_rebuilding_enabled_schedule(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    pool: ConnectionPool,
    fake_session: tuple[_FakeBackend, list[int]],
) -> None:
    """A failed old-generation reap blocks replacement until the official retry wins."""
    from shared.pty_sessions import allocation_freeze

    backend, launched = fake_session
    sid = _insert(db_conn, "generation-retry")
    name = session_name(f"schedule-{sid}")
    backend.live.append(name)
    backend.generations[name] = "previous-generation"
    backend.kill_results = [False, True]
    monkeypatch.setattr(allocation_freeze, "current_generation", lambda: "current-generation")
    manager = sm.ScheduleManager(pool)

    manager._reconcile()
    assert backend.killed == [name]
    assert launched == []
    assert name in backend.live

    manager._reconcile()
    assert backend.killed == [name, name]
    assert launched == [sid]
    assert _status(db_conn, sid) == "running"


def test_launch_env_carries_cluster_timezone(
    db_conn: psycopg.Connection, pool: ConnectionPool, fake_session: tuple[_FakeBackend, list[int]]
) -> None:
    """The runner's spawn env carries the gateway's resolved cluster timezone.

    A unit .env that misses AVA_TIMEZONE must not silently leave the runner on
    the America/Los_Angeles field default (2026-08-21: schedule #3 fired at PT
    midnight instead of Shanghai midnight). The forward dict deliberately
    excludes cluster-scope keys, so the launch pins it explicitly from the
    gateway's own settings — dotenv_boot's authority pass keeps the value
    (never drops it) and lets a declared .env override it."""
    backend, launched = fake_session
    sid = _insert(db_conn, "job-tz")

    sm.ScheduleManager(pool)._launch(sid)

    assert launched == [sid]
    env = backend.launch_envs[0]
    assert env["AVA_TIMEZONE"] == settings.general.timezone
    assert env["AVA_SCHEDULE_ID"] == str(sid)


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


def test_retries_enabled_stale_session_reap_before_launch(
    db_conn: psycopg.Connection, pool: ConnectionPool, fake_session: tuple[_FakeBackend, list[int]]
) -> None:
    """An enabled stale survivor is retried rather than adopted forever."""
    backend, launched = fake_session
    sid = _insert(db_conn, "job-stale-retry")
    stale = session_name(f"schedule-{sid}")
    backend.live.append(stale)
    backend.kill_results = [False, True]
    manager = sm.ScheduleManager(pool)

    manager._launch(sid)
    assert backend.killed == [stale]
    assert launched == []
    assert stale in backend.live

    manager._reconcile()
    assert backend.killed == [stale, stale]
    assert launched == [sid]
    assert stale in backend.live
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
    clock = {"t": 1000.0}
    emitted: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(sm.time, "monotonic", lambda: clock["t"])

    def capture_emit(_category: object, event_name: str, **kwargs: Any) -> None:
        emitted.append((event_name, cast(dict[str, object], kwargs["attributes"])))

    monkeypatch.setattr(sm.telemetry, "emit", capture_emit)
    manager = sm.ScheduleManager(pool)

    with caplog.at_level(logging.WARNING, logger=sm.__name__):
        manager._reconcile()
        clock["t"] += sm._STALL_ALERT_AFTER_S
        manager._reconcile()
        assert emitted == []
        clock["t"] += 0.1
        manager._reconcile()
        manager._reconcile()

        name = session_name(f"schedule-{sid}")
        backend.live.append(name)
        manager._reconcile()
        backend.live.remove(name)
        manager._reconcile()
        clock["t"] += sm._STALL_ALERT_AFTER_S + 0.1
        manager._reconcile()

    assert emitted == [
        (
            "schedule_stalled",
            {"schedule_id": sid, "status": "error", "stalled_seconds": 7200.1},
        ),
        (
            "schedule_stalled",
            {"schedule_id": sid, "status": "error", "stalled_seconds": 7200.1},
        ),
    ]
    assert caplog.text.count(f"schedule {sid} has had no live session") == 2


def test_completed_and_disabled_schedules_do_not_stall_alert(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    fake_session: tuple[_FakeBackend, list[int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed = _insert(db_conn, "completed-no-alert")
    _set_status(db_conn, completed, "completed")
    _insert(db_conn, "disabled-no-alert", enabled=False)
    clock = {"t": 0.0}
    emitted: list[str] = []
    monkeypatch.setattr(sm.time, "monotonic", lambda: clock["t"])

    def capture_emit(_category: object, event_name: str, **_kwargs: Any) -> None:
        emitted.append(event_name)

    monkeypatch.setattr(sm.telemetry, "emit", capture_emit)
    manager = sm.ScheduleManager(pool)

    manager._reconcile()
    clock["t"] += sm._STALL_ALERT_AFTER_S + 1
    manager._reconcile()

    assert emitted == []


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


def test_stop_reaps_then_start_rebuilds_without_reviving_after_gateway_restart(
    db_conn: psycopg.Connection, pool: ConnectionPool, fake_session: tuple[_FakeBackend, list[int]]
) -> None:
    """A stopped schedule has no live PTY, including after a fresh manager.

    The final fresh-manager reconcile models a rollout restart. Its disabled
    desired row prevents revival; re-enabling is the only transition that
    creates a replacement session.
    """
    backend, launched = fake_session
    sid = _insert(db_conn, "stop-start")
    name = session_name(f"schedule-{sid}")
    manager = sm.ScheduleManager(pool)

    manager._sync_blocking(sid)  # enabled -> create
    assert launched == [sid]
    assert name in backend.live
    assert _status(db_conn, sid) == "running"

    with db_conn.cursor() as cur:
        cur.execute("UPDATE schedules SET enabled = false WHERE id = %s", (sid,))
    db_conn.commit()
    manager._sync_blocking(sid)  # disable -> official PTY reap
    assert name in backend.killed
    assert name not in backend.live
    assert _status(db_conn, sid) == "stopped"

    sm.ScheduleManager(pool)._reconcile()  # gateway restart: stopped stays dead
    assert launched == [sid]
    assert name not in backend.live

    with db_conn.cursor() as cur:
        cur.execute("UPDATE schedules SET enabled = true WHERE id = %s", (sid,))
    db_conn.commit()
    manager._sync_blocking(sid)  # explicit start -> create a new PTY
    assert launched == [sid, sid]
    assert name in backend.live
    assert _status(db_conn, sid) == "running"


def test_failed_reap_keeps_runtime_status_and_retries_on_reconcile(
    db_conn: psycopg.Connection, pool: ConnectionPool, fake_session: tuple[_FakeBackend, list[int]]
) -> None:
    """A backend refusal cannot falsely record a dead schedule or run.

    Disabled plus a still-live named session is the durable reap queue: the
    next reconciliation must retry the same official backend operation and
    only then write stopped and interrupt the run history.
    """
    backend, launched = fake_session
    sid = _insert(db_conn, "retry-reap")
    name = session_name(f"schedule-{sid}")
    manager = sm.ScheduleManager(pool)
    manager._sync_blocking(sid)
    run_id = _insert_null_run(db_conn, sid)

    with db_conn.cursor() as cur:
        cur.execute("UPDATE schedules SET enabled = false WHERE id = %s", (sid,))
    db_conn.commit()
    backend.kill_results = [False, True]

    manager._sync_blocking(sid)
    assert name in backend.live
    assert _status(db_conn, sid) == "running"
    assert _run_row(db_conn, run_id) == (None, None)
    assert launched == [sid]

    manager._reconcile()
    assert name not in backend.live
    assert _status(db_conn, sid) == "stopped"
    assert _run_row(db_conn, run_id) == (False, "interrupted")
    assert launched == [sid]


def test_reap_failure_error_logs_are_rate_limited(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    fake_session: tuple[_FakeBackend, list[int]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A persistent reap failure emits periodic ERROR summaries, not a flood."""
    backend, _launched = fake_session
    sid = _insert(db_conn, "rate-limit-reap")
    backend.kill_results = [False, False, False]
    clock = {"t": 1000.0}
    monkeypatch.setattr(sm.time, "monotonic", lambda: clock["t"])
    manager = sm.ScheduleManager(pool)

    with caplog.at_level(logging.ERROR, logger=sm.__name__):
        assert not manager._reap(sid)
        clock["t"] += sm._PTY_FAILURE_LOG_INTERVAL_S - 1
        assert not manager._reap(sid)
        clock["t"] += 1
        assert not manager._reap(sid)

    errors = [
        record.getMessage()
        for record in caplog.records
        if "PTY reap survived" in record.getMessage()
    ]
    assert len(errors) == 2
    assert "suppressed 1 repeats" in errors[1]


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


def test_start_refuses_on_foreign_checkout(
    pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """issue #194: a gateway running from a worktree must not supervise schedules."""
    import asyncio

    monkeypatch.setattr(sm, "prod_service_checkout_error", _refuse_foreign_checkout)
    mgr = sm.ScheduleManager(pool)
    asyncio.run(mgr.start())
    assert mgr._task is None


def _refuse_foreign_checkout(_repo: Path) -> str:
    return "prod home but foreign checkout"
