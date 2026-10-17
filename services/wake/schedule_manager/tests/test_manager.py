from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

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

    def host_generation(self) -> str | None:
        return "host-a"

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
        sid = int(str(env["MEIVA_SCHEDULE_ID"]))
        launched.append(sid)
        backend.launch_envs.append(dict(env))
        backend.live.append(session_name(f"schedule-{sid}"))
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


def _insert_null_run(conn: psycopg.Connection, sid: int) -> int:
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


def test_reconcile_does_not_sweep_orphan_null_rows(
    db_conn: psycopg.Connection, pool: ConnectionPool, fake_session: tuple[_FakeBackend, list[int]]
) -> None:
    _backend, launched = fake_session
    sid = _insert(db_conn, "orphan-a", enabled=False)
    rid = _insert_null_run(db_conn, sid)

    sm.ScheduleManager(pool).reconcile()

    assert launched == []
    assert _run_row(db_conn, rid) == (None, None)


def test_reconcile_keeps_null_row_for_live_session(
    db_conn: psycopg.Connection, pool: ConnectionPool, fake_session: tuple[_FakeBackend, list[int]]
) -> None:
    backend, launched = fake_session
    sid = _insert(db_conn, "orphan-b")
    rid = _insert_null_run(db_conn, sid)
    backend.live.append(session_name(f"schedule-{sid}"))

    sm.ScheduleManager(pool).reconcile()

    assert launched == []
    assert _run_row(db_conn, rid) == (None, None)


def test_launch_closes_null_rows_of_dead_predecessor(
    db_conn: psycopg.Connection, pool: ConnectionPool, fake_session: tuple[_FakeBackend, list[int]]
) -> None:
    _backend, launched = fake_session
    sid = _insert(db_conn, "orphan-c")
    rid = _insert_null_run(db_conn, sid)

    sm.ScheduleManager(pool).reconcile()

    assert launched == [sid]
    assert _run_row(db_conn, rid) == (False, "interrupted")


def test_sync_stop_closes_orphan_rows_immediately(
    db_conn: psycopg.Connection, pool: ConnectionPool, fake_session: tuple[_FakeBackend, list[int]]
) -> None:
    backend, launched = fake_session
    sid = _insert(db_conn, "orphan-d", enabled=True)
    rid = _insert_null_run(db_conn, sid)
    backend.live.append(session_name(f"schedule-{sid}"))
    with db_conn.cursor() as cur:
        cur.execute("UPDATE schedules SET enabled = false WHERE id = %s", (sid,))
    db_conn.commit()

    sm.ScheduleManager(pool).sync_one(sid)

    assert session_name(f"schedule-{sid}") in backend.killed
    assert launched == []
    assert _run_row(db_conn, rid) == (False, "interrupted")


def test_launches_enabled_missing_session(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    pool: ConnectionPool,
    fake_session: tuple[_FakeBackend, list[int]],
) -> None:
    from base.sessions.pty import allocation_freeze

    _backend, launched = fake_session
    monkeypatch.setattr(allocation_freeze, "current_generation", lambda: "current-generation")
    sid = _insert(db_conn, "job-a")

    sm.ScheduleManager(pool).reconcile()

    assert launched == [sid]
    assert _status(db_conn, sid) == "running"


def test_reconcile_defers_enabled_launch_until_the_host_is_serving(
    serving_root: start_serving.RootBirth,
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    fake_session: tuple[_FakeBackend, list[int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    _backend, launched = fake_session
    start_serving.clear_serving()
    sid = _insert(db_conn, "await-serving")
    manager = sm.ScheduleManager(pool)

    manager.reconcile()

    assert launched == []
    assert _status(db_conn, sid) == "stopped"

    generation = start_serving.begin_start()
    assert start_serving.mark_serving(generation, runtime=serving_root.runtime) is True
    manager.reconcile()

    assert launched == [sid]
    assert _status(db_conn, sid) == "running"


def test_live_superseded_generation_is_reaped_before_schedule_reconcile(
    monkeypatch: pytest.MonkeyPatch,
    pool: ConnectionPool,
    fake_session: tuple[_FakeBackend, list[int]],
) -> None:
    from base.sessions.pty import allocation_freeze

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
    from base.sessions.pty import allocation_freeze

    backend, launched = fake_session
    sid = _insert(db_conn, "generation-retry")
    name = session_name(f"schedule-{sid}")
    backend.live.append(name)
    backend.generations[name] = "previous-generation"
    backend.kill_results = [False, True]
    monkeypatch.setattr(allocation_freeze, "current_generation", lambda: "current-generation")
    manager = sm.ScheduleManager(pool)

    manager.reconcile()
    assert backend.killed == [name]
    assert launched == []
    assert name in backend.live

    manager.reconcile()
    assert backend.killed == [name, name]
    assert launched == [sid]
    assert _status(db_conn, sid) == "running"


def test_launch_env_carries_cluster_timezone(
    db_conn: psycopg.Connection, pool: ConnectionPool, fake_session: tuple[_FakeBackend, list[int]]
) -> None:
    backend, launched = fake_session
    sid = _insert(db_conn, "job-tz")

    sm.ScheduleManager(pool)._launch(sid)

    assert launched == [sid]
    env = backend.launch_envs[0]
    assert env["MEIVA_TIMEZONE"] == settings.general.timezone
    assert env["MEIVA_SCHEDULE_ID"] == str(sid)


def test_adopts_live_session_without_relaunch(
    db_conn: psycopg.Connection, pool: ConnectionPool, fake_session: tuple[_FakeBackend, list[int]]
) -> None:
    backend, launched = fake_session
    sid = _insert(db_conn, "job-b")
    backend.live.append(session_name(f"schedule-{sid}"))

    sm.ScheduleManager(pool).reconcile()

    assert launched == []


def test_reaps_stale_session_before_launch(
    db_conn: psycopg.Connection, pool: ConnectionPool, fake_session: tuple[_FakeBackend, list[int]]
) -> None:
    backend, launched = fake_session
    sid = _insert(db_conn, "job-stale")
    stale = session_name(f"schedule-{sid}")
    backend.live.append(stale)

    sm.ScheduleManager(pool)._launch(sid)

    assert stale in backend.killed
    assert launched == [sid]
    assert _status(db_conn, sid) == "running"


def test_retries_enabled_stale_session_reap_before_launch(
    db_conn: psycopg.Connection, pool: ConnectionPool, fake_session: tuple[_FakeBackend, list[int]]
) -> None:
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

    manager.reconcile()
    assert backend.killed == [stale, stale]
    assert launched == [sid]
    assert stale in backend.live
    assert _status(db_conn, sid) == "running"


def test_kills_orphan_session(
    db_conn: psycopg.Connection, pool: ConnectionPool, fake_session: tuple[_FakeBackend, list[int]]
) -> None:
    backend, _launched = fake_session
    sid = _insert(db_conn, "job-c", enabled=False)
    orphan = session_name(f"schedule-{sid}")
    backend.live.append(orphan)

    sm.ScheduleManager(pool).reconcile()

    assert orphan in backend.killed
    assert _status(db_conn, sid) == "stopped"


def _set_status(conn: psycopg.Connection, sid: int, status: str) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE schedules SET status = %s WHERE id = %s", (status, sid))
    conn.commit()


def test_completed_schedule_not_relaunched(
    db_conn: psycopg.Connection, pool: ConnectionPool, fake_session: tuple[_FakeBackend, list[int]]
) -> None:
    _backend, launched = fake_session
    sid = _insert(db_conn, "done-a")
    _set_status(db_conn, sid, "completed")

    sm.ScheduleManager(pool).reconcile()

    assert launched == []
    assert _status(db_conn, sid) == "completed"


def test_sync_recovers_errored_schedule(
    db_conn: psycopg.Connection, pool: ConnectionPool, fake_session: tuple[_FakeBackend, list[int]]
) -> None:
    backend, launched = fake_session
    sid = _insert(db_conn, "err-b")
    _set_status(db_conn, sid, "error")
    db_conn.execute(
        "UPDATE schedules SET desired_revision = desired_revision + 1 WHERE id = %s", (sid,)
    )
    db_conn.commit()

    sm.ScheduleManager(pool).sync_one(sid)

    assert launched == [sid]
    assert session_name(f"schedule-{sid}") in backend.live
    assert _status(db_conn, sid) == "running"


def test_sync_launches_enabled_and_kills_disabled(
    db_conn: psycopg.Connection, pool: ConnectionPool, fake_session: tuple[_FakeBackend, list[int]]
) -> None:
    backend, launched = fake_session
    sid = _insert(db_conn, "sync-a", enabled=True)
    mgr = sm.ScheduleManager(pool)

    mgr.sync_one(sid)
    assert launched == [sid]
    assert session_name(f"schedule-{sid}") in backend.live

    with db_conn.cursor() as cur:
        cur.execute("UPDATE schedules SET enabled = false WHERE id = %s", (sid,))
    db_conn.commit()
    mgr.sync_one(sid)
    assert session_name(f"schedule-{sid}") in backend.killed
    assert launched == [sid]


def test_stop_reaps_then_start_rebuilds_without_reviving_after_gateway_restart(
    db_conn: psycopg.Connection, pool: ConnectionPool, fake_session: tuple[_FakeBackend, list[int]]
) -> None:
    backend, launched = fake_session
    sid = _insert(db_conn, "stop-start")
    name = session_name(f"schedule-{sid}")
    manager = sm.ScheduleManager(pool)

    manager.sync_one(sid)
    assert launched == [sid]
    assert name in backend.live
    assert _status(db_conn, sid) == "running"

    with db_conn.cursor() as cur:
        cur.execute("UPDATE schedules SET enabled = false WHERE id = %s", (sid,))
    db_conn.commit()
    manager.sync_one(sid)
    assert name in backend.killed
    assert name not in backend.live
    assert _status(db_conn, sid) == "stopped"

    sm.ScheduleManager(pool).reconcile()
    assert launched == [sid]
    assert name not in backend.live

    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE schedules SET enabled = true, desired_revision = desired_revision + 1, launch_count = 0 WHERE id = %s",
            (sid,),
        )
    db_conn.commit()
    manager.sync_one(sid)
    assert launched == [sid, sid]
    assert name in backend.live
    assert _status(db_conn, sid) == "running"


def test_failed_reap_keeps_runtime_status_and_retries_on_reconcile(
    db_conn: psycopg.Connection, pool: ConnectionPool, fake_session: tuple[_FakeBackend, list[int]]
) -> None:
    backend, launched = fake_session
    sid = _insert(db_conn, "retry-reap")
    name = session_name(f"schedule-{sid}")
    manager = sm.ScheduleManager(pool)
    manager.sync_one(sid)
    run_id = _insert_null_run(db_conn, sid)

    with db_conn.cursor() as cur:
        cur.execute("UPDATE schedules SET enabled = false WHERE id = %s", (sid,))
    db_conn.commit()
    backend.kill_results = [False, True]

    manager.sync_one(sid)
    assert name in backend.live
    assert _status(db_conn, sid) == "running"
    assert _run_row(db_conn, run_id) == (None, None)
    assert launched == [sid]

    manager.reconcile()
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
    backend, _launched = fake_session
    sid = _insert(db_conn, "rate-limit-reap")
    backend.kill_results = [False, False, False]
    clock = {"t": 1000.0}
    monkeypatch.setattr(sm.time, "monotonic", lambda: clock["t"])
    manager = sm.ScheduleManager(pool)

    with caplog.at_level(logging.ERROR, logger=sm.__name__):
        assert not manager._reap(sid)
        clock["t"] += sm.PTY_FAILURE_LOG_INTERVAL_S - 1
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


def test_schedule_manager_uses_shell_backend() -> None:
    from base.sessions.backend import get_backend, get_shell_backend

    assert sm.get_shell_backend is get_shell_backend
    assert sm.get_shell_backend is not get_backend


def test_successful_launch_clears_stale_last_error(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    fake_session: tuple[_FakeBackend, list[int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    _set_status(db_conn, sid, "stopped")

    clock = {"t": 0.0}
    monkeypatch.setattr(sm.time, "monotonic", lambda: clock["t"])
    mgr = sm.ScheduleManager(pool)
    mgr.reconcile()
    assert launched == [sid]
    with db_conn.cursor() as cur:
        cur.execute("SELECT status, last_error FROM schedules WHERE id = %s", (sid,))
        row = cur.fetchone()
    assert row is not None
    status, last_error = row
    assert status == "running"
    assert last_error is None, "stale breaker text must be cleared on successful launch"


def _clock(monkeypatch: pytest.MonkeyPatch) -> dict[str, float]:
    clock = {"t": 1000.0}
    monkeypatch.setattr(sm.time, "monotonic", lambda: clock["t"])
    return clock


def _tick(manager: sm.ScheduleManager, clock: dict[str, float], seconds: float) -> None:
    end = clock["t"] + seconds
    while clock["t"] < end:
        manager.reconcile()
        clock["t"] += 30


def _last_error(conn: psycopg.Connection, sid: int) -> str | None:
    row = conn.execute("SELECT last_error FROM schedules WHERE id = %s", (sid,)).fetchone()
    assert row is not None
    return row[0]


def test_a_reap_that_keeps_failing_is_given_up_once_the_window_ends(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    fake_session: tuple[_FakeBackend, list[int]],
    monkeypatch: pytest.MonkeyPatch,
    emitted_events: EmittedEvents,
) -> None:
    backend, launched = fake_session
    clock = _clock(monkeypatch)
    sid = _insert(db_conn, "survivor", enabled=False)
    name = session_name(f"schedule-{sid}")
    backend.live.append(name)
    backend.kill_results = [False] * 10_000
    manager = sm.ScheduleManager(pool)

    _tick(manager, clock, sm.BLOCKED_GIVE_UP_AFTER_S - 30)
    retried = len(backend.killed)
    assert retried == sm.BLOCKED_GIVE_UP_AFTER_S // 30 - 1
    assert len(emitted_events) == 0
    assert _status(db_conn, sid) == "running"

    clock["t"] += 30
    manager.reconcile()
    assert len(backend.killed) == retried + 1
    assert _status(db_conn, sid) == "error"
    assert "gave up" in (_last_error(db_conn, sid) or "")
    assert [(event.event_name, event.attributes["schedule_id"]) for event in emitted_events] == [
        ("schedule_convergence_abandoned", sid)
    ]

    for _ in range(3):
        clock["t"] += 30
        manager.reconcile()
        assert manager.sync_one(sid) is True
    assert len(backend.killed) == retried + 1
    assert len(emitted_events) == 1
    assert name in backend.live
    assert launched == []


@pytest.mark.usefixtures("emitted_events")
def test_an_explicit_new_revision_lifts_the_give_up(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    fake_session: tuple[_FakeBackend, list[int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, _launched = fake_session
    clock = _clock(monkeypatch)
    sid = _insert(db_conn, "survivor-lifted", enabled=False)
    backend.live.append(session_name(f"schedule-{sid}"))
    backend.kill_results = [False] * 10_000
    manager = sm.ScheduleManager(pool)
    _tick(manager, clock, sm.BLOCKED_GIVE_UP_AFTER_S + 30)
    assert _status(db_conn, sid) == "error"
    attempts = len(backend.killed)

    backend.kill_results = []
    db_conn.execute(
        "UPDATE schedules SET desired_revision = desired_revision + 1 WHERE id = %s", (sid,)
    )
    db_conn.commit()
    assert manager.sync_one(sid) is True

    assert len(backend.killed) == attempts + 1
    assert _status(db_conn, sid) == "stopped"
    assert sid not in manager._abandoned


def test_a_failure_streak_is_broken_by_a_gap(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    fake_session: tuple[_FakeBackend, list[int]],
    monkeypatch: pytest.MonkeyPatch,
    emitted_events: EmittedEvents,
) -> None:
    clock = _clock(monkeypatch)
    sid = _insert(db_conn, "gappy")
    manager = sm.ScheduleManager(pool)

    for _ in range(4):
        manager._note_blocked(sid, "x")
        clock["t"] += sm.BLOCKED_STREAK_GAP_S + 1

    assert len(emitted_events) == 0
    assert sid not in manager._abandoned
