"""Convergence retries recover the existing revision, including acknowledgement loss."""

from pathlib import Path

import psycopg
import pytest

import base.db
from base.cluster import session_name
from base.deploy.lifecycle import start_serving
from base.sessions.pty.client import SessionInfo
from gateway.schedules import session_control
from services.wake.schedule_manager import manager, requests, revisions


class Backend:
    def __init__(self) -> None:
        self.commands: dict[str, str] = {}
        self.killed: list[str] = []
        self.launches = 0
        self.fail_launch = False
        self.fail_reap = False

    def list_sessions(self, prefix: str = "") -> list[str]:
        return [name for name in self.commands if name.startswith(prefix)]

    def has_session(self, name: str) -> bool:
        return name in self.commands

    def session_generation(self, name: str) -> str | None:
        return None

    def kill_session(self, name: str) -> tuple[bool, str]:
        self.killed.append(name)
        if self.fail_reap:
            return False, "survived"
        self.commands.pop(name, None)
        return True, "forced"

    def new_session(self, name: str, cmd: str, cwd: object, *, env: dict[str, str]) -> bool:
        self.launches += 1
        if not self.fail_launch:
            self.commands[name] = cmd
        return not self.fail_launch


@pytest.fixture
def backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, serving_root: start_serving.RootBirth
) -> Backend:
    from base.deploy.maintenance import admission
    from base.sessions.pty import allocation_freeze

    backend = Backend()
    monkeypatch.setattr(start_serving, "state_path", lambda: tmp_path / "start-serving.json")
    generation = start_serving.begin_start()
    assert start_serving.mark_serving(generation, runtime=serving_root.runtime)
    monkeypatch.setattr(admission, "held", lambda: False)
    monkeypatch.setattr(manager, "get_shell_backend", lambda: backend)
    monkeypatch.setattr(allocation_freeze, "current_generation", lambda: None)

    def infos(prefix: str = "", *, include_initial_command: bool = False) -> list[SessionInfo]:
        return [
            SessionInfo(
                name,
                1,
                1.0,
                None,
                "bash",
                str(tmp_path),
                1.0,
                None,
                cmd if include_initial_command else None,
            )
            for name, cmd in backend.commands.items()
            if name.startswith(prefix)
        ]

    monkeypatch.setattr(revisions.client, "list_sessions", infos)
    return backend


def _insert(conn: psycopg.Connection) -> int:
    row = conn.execute(
        "INSERT INTO schedules (name, script, command, desired_revision) VALUES ('revision', 'pass', 'python schedule.py', 1) RETURNING id"
    ).fetchone()
    assert row is not None
    conn.commit()
    return row[0]


def test_acknowledgement_loss_adopts_session_across_manager_restart(
    db_conn: psycopg.Connection, backend: Backend
) -> None:
    sid = _insert(db_conn)
    with base.db.pool() as pool:
        session_control.enqueue_blocking(pool, sid)
        assert manager.ScheduleManager(pool).sync_one(sid)
        # Simulate crash after convergence but before queue acknowledgement.
        assert requests.consume_requests(pool, manager.ScheduleManager(pool)) == 1
    assert backend.launches == 1
    assert backend.killed == []
    assert db_conn.execute(
        "SELECT desired_revision, applied_revision FROM schedules WHERE id = %s", (sid,)
    ).fetchone() == (1, 1)


def test_crash_after_session_birth_before_applied_write_adopts_provenance(
    db_conn: psycopg.Connection, backend: Backend
) -> None:
    sid = _insert(db_conn)
    backend.commands[session_name(f"schedule-{sid}")] = (
        f".venv/bin/python -m gateway.schedules.runner {sid} 1; exit $?"
    )
    with base.db.pool() as pool:
        assert manager.ScheduleManager(pool).sync_one(sid)
    assert backend.launches == 0
    assert backend.killed == []
    assert db_conn.execute(
        "SELECT applied_revision FROM schedules WHERE id = %s", (sid,)
    ).fetchone() == (1,)


def test_completed_applied_revision_is_not_rerun_by_unacknowledged_queue(
    db_conn: psycopg.Connection, backend: Backend
) -> None:
    sid = _insert(db_conn)
    db_conn.execute(
        "UPDATE schedules SET applied_revision = 1, status = 'completed' WHERE id = %s", (sid,)
    )
    db_conn.commit()
    with base.db.pool() as pool:
        session_control.enqueue_blocking(pool, sid)
        assert requests.consume_requests(pool, manager.ScheduleManager(pool)) == 1
    assert backend.launches == 0
    assert backend.killed == []


def test_reap_failure_keeps_convergence_pending(
    db_conn: psycopg.Connection, backend: Backend
) -> None:
    sid = _insert(db_conn)
    backend.commands[session_name(f"schedule-{sid}")] = (
        f".venv/bin/python -m gateway.schedules.runner {sid} 0; exit $?"
    )
    backend.fail_reap = True
    with base.db.pool() as pool:
        session_control.enqueue_blocking(pool, sid)
        assert requests.consume_requests(pool, manager.ScheduleManager(pool)) == 0
    assert backend.launches == 0
    assert db_conn.execute("SELECT schedule_id FROM schedule_sync_requests").fetchall() == [(sid,)]


def test_failed_revision_keeps_backoff_across_consumer_retries(
    db_conn: psycopg.Connection, backend: Backend
) -> None:
    sid = _insert(db_conn)
    backend.fail_launch = True
    with base.db.pool() as pool:
        assert not manager.ScheduleManager(pool).sync_one(sid)
        assert not manager.ScheduleManager(pool).sync_one(sid)
    assert backend.launches == 1
    assert db_conn.execute(
        "SELECT launch_count, applied_revision FROM schedules WHERE id = %s", (sid,)
    ).fetchone() == (1, 0)


def test_an_uncertain_live_revision_is_never_blindly_replaced(
    db_conn: psycopg.Connection, backend: Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = _insert(db_conn)
    backend.commands[session_name(f"schedule-{sid}")] = (
        f".venv/bin/python -m gateway.schedules.runner {sid} 1; exit $?"
    )

    def unavailable(_prefix: str = "", **_kw: object) -> list[SessionInfo]:
        return []

    monkeypatch.setattr(revisions.client, "list_sessions", unavailable)
    with base.db.pool() as pool:
        session_control.enqueue_blocking(pool, sid)
        assert requests.consume_requests(pool, manager.ScheduleManager(pool)) == 0
    assert backend.launches == 0
    assert backend.killed == []
    assert db_conn.execute("SELECT schedule_id FROM schedule_sync_requests").fetchall() == [(sid,)]


def test_a_failed_schedule_does_not_block_other_queued_revisions(
    db_conn: psycopg.Connection, backend: Backend
) -> None:
    sid = _insert(db_conn)
    backend.commands[session_name(f"schedule-{sid}")] = "legacy"
    backend.fail_reap = True
    row = db_conn.execute(
        "INSERT INTO schedules (name, script, command, desired_revision) VALUES ('other', 'pass', 'python schedule.py', 1) RETURNING id"
    ).fetchone()
    assert row is not None
    other = row[0]
    db_conn.commit()
    with base.db.pool() as pool:
        session_control.enqueue_blocking(pool, sid)
        session_control.enqueue_blocking(pool, other)
        assert requests.consume_requests(pool, manager.ScheduleManager(pool)) == 1
    assert db_conn.execute("SELECT schedule_id FROM schedule_sync_requests").fetchall() == [(sid,)]
    assert backend.launches == 1


def test_independent_managers_cannot_own_one_schedule_concurrently(
    db_conn: psycopg.Connection, backend: Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    sid = _insert(db_conn)
    started = Event()
    release = Event()
    allocate = backend.new_session

    def blocked(name: str, cmd: str, cwd: object, *, env: dict[str, str]) -> bool:
        result = allocate(name, cmd, cwd, env=env)
        started.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(backend, "new_session", blocked)
    with base.db.pool(max_size=4) as pool, ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(manager.ScheduleManager(pool).sync_one, sid)
        try:
            assert started.wait(5)
            second = workers.submit(manager.ScheduleManager(pool).sync_one, sid)
            assert second.result(timeout=3) is False
        finally:
            release.set()
        assert first.result(timeout=3) is True
    assert backend.launches == 1
    assert backend.killed == []


def test_newer_edit_during_launch_is_not_acknowledged_as_applied(
    db_conn: psycopg.Connection, backend: Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gateway.schedules import router

    sid = _insert(db_conn)
    allocate = backend.new_session
    with base.db.pool(max_size=4) as pool:

        def edit_during_launch(name: str, cmd: str, cwd: object, *, env: dict[str, str]) -> bool:
            result = allocate(name, cmd, cwd, env=env)
            router._update_blocking(pool, sid, {"script": "print(2)"})
            return result

        monkeypatch.setattr(backend, "new_session", edit_during_launch)
        assert not manager.ScheduleManager(pool).sync_one(sid)
        assert db_conn.execute(
            "SELECT desired_revision, applied_revision FROM schedules WHERE id = %s", (sid,)
        ).fetchone() == (2, 0)
        monkeypatch.setattr(backend, "new_session", allocate)
        assert manager.ScheduleManager(pool).sync_one(sid)
    assert db_conn.execute(
        "SELECT desired_revision, applied_revision FROM schedules WHERE id = %s", (sid,)
    ).fetchone() == (2, 2)
    assert backend.launches == 2
    assert backend.killed == [session_name(f"schedule-{sid}")]


def test_old_reap_retry_cannot_kill_a_matching_applied_replacement(
    db_conn: psycopg.Connection, backend: Backend
) -> None:
    sid = _insert(db_conn)
    with base.db.pool() as pool:
        mgr = manager.ScheduleManager(pool)
        assert mgr.sync_one(sid)
        mgr._reap_retries.add(sid)
        mgr.reconcile()
    assert backend.launches == 1
    assert backend.killed == []
