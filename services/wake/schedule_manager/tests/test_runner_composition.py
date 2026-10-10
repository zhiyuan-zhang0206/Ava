"""The schedule process composes SDK inputs at the execution boundaries."""

import subprocess
import sys
import threading
from pathlib import Path

import psycopg
import pytest

from services.wake.schedule_manager import runner


def _schedule(conn: psycopg.Connection) -> int:
    row = conn.execute(
        "INSERT INTO schedules (name, script, command) "
        "VALUES ('composition', 'pass', 'python schedule.py') RETURNING id"
    ).fetchone()
    conn.commit()
    assert row is not None
    return row[0]


def test_actor_binding_failure_does_not_open_history(
    db_conn: psycopg.Connection, unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = _schedule(db_conn)
    error = RuntimeError("actor unavailable")

    def bind(schedule_id: int) -> None:
        assert schedule_id == sid
        assert (unit_home / "schedules" / str(sid) / "schedule.py").read_text() == "pass"
        raise error

    monkeypatch.setattr(runner, "_bind_schedule_actor", bind)
    with pytest.raises(RuntimeError) as caught:
        runner.run(sid)
    assert caught.value is error
    assert db_conn.execute("SELECT count(*) FROM schedule_runs").fetchone() == (0,)


def test_plugins_load_under_guard_after_actor_and_history(
    db_conn: psycopg.Connection, unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = _schedule(db_conn)
    guards: list[threading.Thread] = []

    def plugins() -> None:
        from ava.sdk_surface.agent_identity import require_actor

        assert require_actor() == f"schedule:{sid}"
        assert (unit_home / "schedules" / str(sid) / "schedule.py").exists()
        assert db_conn.execute("SELECT ok FROM schedule_runs").fetchall() == [(None,)]
        guards.extend(t for t in threading.enumerate() if t.name == f"schedule-{sid}-stall-guard")
        assert len(guards) == 1 and guards[0].is_alive()
        raise RuntimeError("plugin failure")

    monkeypatch.setattr(runner.ava, "ensure_plugins_loaded", plugins)
    assert runner.run(sid) == 1
    assert not guards[0].is_alive()
    assert db_conn.execute("SELECT ok FROM schedule_runs").fetchall() == [(False,)]


def test_retired_entrypoint_fails_without_execution(unit_home: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "gateway.schedules.runner", "1"],
        cwd=Path(__file__).resolve().parents[4],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 1
    assert "services.wake.schedule_manager.runner" in result.stderr
    assert not (unit_home / "schedules").exists()


def test_main_refuses_on_foreign_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(_repo: Path) -> str:
        return "foreign checkout"

    monkeypatch.setattr(sys, "argv", ["schedule_runner", "1"])
    monkeypatch.setattr(runner, "prod_service_checkout_error", refuse)
    with pytest.raises(SystemExit) as caught:
        runner.main()
    assert caught.value.code == 3
