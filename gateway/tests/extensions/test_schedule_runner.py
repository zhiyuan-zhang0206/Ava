from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import psycopg
import pytest

from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config import settings
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from gateway.schedules.tests.runner_inputs import run_schedule as run
from services.wake.schedule_manager.engine import script_filename


@pytest.fixture(autouse=True)
def _restore_actor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AVA_SCHEDULE_ID", raising=False)


def _insert_schedule(
    conn: psycopg.Connection,
    *,
    script: str,
    command: str = "python schedule.py",
    enabled: bool = True,
    name: str = "s",
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO schedules (name, script, command, enabled, status) "
            "VALUES (%s, %s, %s, %s, 'stopped') RETURNING id",
            (name, script, command, enabled),
        )
        row = cur.fetchone()
    conn.commit()
    assert row is not None
    return row[0]


def _status_and_error(conn: psycopg.Connection, sid: int) -> tuple[str, str | None]:
    with conn.cursor() as cur:
        cur.execute("SELECT status, last_error FROM schedules WHERE id = %s", (sid,))
        row = cur.fetchone()
    assert row is not None
    return row[0], row[1]


def test_run_materializes_and_executes_and_binds_actor(
    db_conn: psycopg.Connection, unit_home: Path, *, machine_identity: MachineIdentity
) -> None:
    marker = unit_home / "ran.txt"
    script = f"import ava.sdk_surface.agent_identity, pathlib\npathlib.Path({str(marker)!r}).write_text(ava.sdk_surface.agent_identity.require_actor())\n"
    sid = _insert_schedule(db_conn, script=script)

    rc = run(sid, machine=machine_identity)

    assert rc == 0
    assert (unit_home / "schedules" / str(sid) / "schedule.py").read_text() == script
    assert marker.read_text() == f"schedule:{sid}"


def test_run_marks_completed_on_clean_py_exit(
    db_conn: psycopg.Connection, unit_home: Path, *, machine_identity: MachineIdentity
) -> None:
    sid = _insert_schedule(db_conn, script="x = 1 + 1\n")

    rc = run(sid, machine=machine_identity)

    assert rc == 0
    status, last_error = _status_and_error(db_conn, sid)
    assert status == "completed"
    assert last_error is None


def test_run_marks_completed_on_clean_subprocess_exit(
    db_conn: psycopg.Connection, unit_home: Path, *, machine_identity: MachineIdentity
) -> None:
    sid = _insert_schedule(db_conn, script="exit 0\n", command="bash run.sh")

    rc = run(sid, machine=machine_identity)

    assert rc == 0
    status, _ = _status_and_error(db_conn, sid)
    assert status == "completed"


def test_run_captures_crash_to_last_error(
    db_conn: psycopg.Connection, unit_home: Path, *, machine_identity: MachineIdentity
) -> None:
    sid = _insert_schedule(db_conn, script="raise RuntimeError('boom-42')\n")

    rc = run(sid, machine=machine_identity)

    assert rc == 1
    status, last_error = _status_and_error(db_conn, sid)
    assert last_error is not None and "boom-42" in last_error
    assert status != "completed"


def test_run_nonzero_subprocess_is_not_completed(
    db_conn: psycopg.Connection, unit_home: Path, *, machine_identity: MachineIdentity
) -> None:
    sid = _insert_schedule(db_conn, script="exit 3\n", command="bash run.sh")

    rc = run(sid, machine=machine_identity)

    assert rc == 3
    status, last_error = _status_and_error(db_conn, sid)
    assert status != "completed"
    assert last_error is not None and "exited 3" in last_error


def _runs(conn: psycopg.Connection, sid: int) -> list[tuple[bool | None, str | None]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ok, note FROM schedule_runs WHERE schedule_id = %s ORDER BY id",
            (sid,),
        )
        return [(r[0], r[1]) for r in cur.fetchall()]


def test_run_records_clean_py_exit(
    db_conn: psycopg.Connection, unit_home: Path, *, machine_identity: MachineIdentity
) -> None:
    sid = _insert_schedule(db_conn, script="x = 1 + 1\n")

    assert run(sid, machine=machine_identity) == 0
    assert _runs(db_conn, sid) == [(True, None)]


def test_run_records_crash(
    db_conn: psycopg.Connection, unit_home: Path, *, machine_identity: MachineIdentity
) -> None:
    sid = _insert_schedule(db_conn, script="raise RuntimeError('x' * 5000 + 'the-end')\n")

    assert run(sid, machine=machine_identity) == 1
    [(ok, note)] = _runs(db_conn, sid)
    assert ok is False and note is not None and len(note) <= 3100
    assert note.splitlines()[0] == "crashed: RuntimeError" and note.endswith("the-end")


def test_run_records_nonzero_command(
    db_conn: psycopg.Connection, unit_home: Path, *, machine_identity: MachineIdentity
) -> None:
    sid = _insert_schedule(db_conn, script="exit 3\n", command="bash run.sh")

    assert run(sid, machine=machine_identity) == 3
    assert _runs(db_conn, sid) == [(False, "command exited 3")]


def test_run_records_py_sys_exit_zero(
    db_conn: psycopg.Connection, unit_home: Path, *, machine_identity: MachineIdentity
) -> None:
    sid = _insert_schedule(db_conn, script="raise SystemExit(0)\n")

    assert run(sid, machine=machine_identity) == 0
    status, last_error = _status_and_error(db_conn, sid)
    assert status == "completed"
    assert last_error is None
    assert _runs(db_conn, sid) == [(True, None)]


def test_run_records_py_sys_exit_nonzero(
    db_conn: psycopg.Connection, unit_home: Path, *, machine_identity: MachineIdentity
) -> None:
    sid = _insert_schedule(db_conn, script="raise SystemExit(3)\n")

    assert run(sid, machine=machine_identity) == 3
    status, last_error = _status_and_error(db_conn, sid)
    assert status != "completed"
    assert last_error is not None and "script exited 3" in last_error
    assert _runs(db_conn, sid) == [(False, "script exited 3")]


def test_agent_status_guard_failure_is_visible_in_schedule_records(
    db_conn: psycopg.Connection, unit_home: Path, *, machine_identity: MachineIdentity
) -> None:
    script = """
from schedules.agent_status_guard import ensure_agent_status_members

class DriftedAgentStatus:
    RUNNING = "running"

ensure_agent_status_members(
    DriftedAgentStatus,
    {"IDLING", "RUNNING"},
    schedule_name="drift-probe",
)
"""
    sid = _insert_schedule(db_conn, script=script, name="drift-probe")

    assert run(sid, machine=machine_identity) == 1
    status, last_error = _status_and_error(db_conn, sid)
    expected = "schedule 'drift-probe' is missing AgentStatus members: IDLING"
    assert status != "completed"
    assert last_error is not None and expected in last_error
    assert _runs(db_conn, sid) == [(False, f"script exited 1: {expected}")]


@pytest.mark.parametrize(
    ("exit_expr", "expected_note"),
    [
        ("'done early'", "script exited 1: done early"),
        ("3.7", "script exited 1: 3.7"),
    ],
)
def test_run_records_py_sys_exit_message(
    db_conn: psycopg.Connection,
    unit_home: Path,
    exit_expr: str,
    expected_note: str,
    *,
    machine_identity: MachineIdentity,
) -> None:
    sid = _insert_schedule(db_conn, script=f"raise SystemExit({exit_expr})\n")

    assert run(sid, machine=machine_identity) == 1
    assert _runs(db_conn, sid) == [(False, expected_note)]


@pytest.mark.parametrize(
    ("exit_expr", "expected_rc", "expected_rows"),
    [
        ("True", 1, [(False, "script exited 1")]),
        ("False", 0, [(True, None)]),
    ],
)
def test_run_records_py_sys_exit_bool(
    db_conn: psycopg.Connection,
    unit_home: Path,
    exit_expr: str,
    expected_rc: int,
    expected_rows: list[tuple[bool | None, str | None]],
    *,
    machine_identity: MachineIdentity,
) -> None:
    sid = _insert_schedule(db_conn, script=f"raise SystemExit({exit_expr})\n")

    assert run(sid, machine=machine_identity) == expected_rc
    assert _runs(db_conn, sid) == expected_rows


def test_run_records_stall_timeout(
    db_conn: psycopg.Connection,
    unit_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    machine_identity: MachineIdentity,
) -> None:

    monkeypatch.setattr(settings.gateway, "schedule_stall_timeout_seconds", 2.0)
    sid = _insert_schedule(db_conn, script="sleep 60\n", command="bash run.sh")

    assert run(sid, machine=machine_identity) == 1
    assert _runs(db_conn, sid) == [(False, "stall timeout (2s)")]


def test_run_skips_disabled_schedule(
    db_conn: psycopg.Connection, unit_home: Path, *, machine_identity: MachineIdentity
) -> None:
    sid = _insert_schedule(db_conn, script="raise RuntimeError('should not run')\n", enabled=False)
    assert run(sid, machine=machine_identity) == 0
    assert not (unit_home / "schedules" / str(sid)).exists()
    with db_conn.cursor() as cur:
        cur.execute("SELECT last_error FROM schedules WHERE id = %s", (sid,))
        row = cur.fetchone()
    assert row is not None and row[0] is None
    assert _runs(db_conn, sid) == []


def test_run_loads_plugins_for_py_script(
    db_conn: psycopg.Connection,
    unit_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    machine_identity: MachineIdentity,
) -> None:
    import ava

    calls: list[int] = []
    monkeypatch.setattr(ava, "ensure_plugins_loaded", lambda: calls.append(1))
    sid = _insert_schedule(db_conn, script="x = 1\n")

    assert run(sid, machine=machine_identity) == 0
    assert calls == [1]


def test_run_hands_py_script_a_clean_argv(
    db_conn: psycopg.Connection,
    unit_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    machine_identity: MachineIdentity,
) -> None:
    monkeypatch.setattr(sys, "argv", ["schedule_runner", "4242"])
    marker = unit_home / "argv.json"
    script = (
        "import json, pathlib, sys\n"
        f"pathlib.Path({str(marker)!r}).write_text(json.dumps(sys.argv))\n"
    )
    sid = _insert_schedule(db_conn, script=script)

    assert run(sid, machine=machine_identity) == 0
    assert json.loads(marker.read_text()) == [
        str(unit_home / "schedules" / str(sid) / "schedule.py")
    ]
    assert sys.argv == ["schedule_runner", "4242"]


def test_run_skips_plugin_load_for_non_py_command(
    db_conn: psycopg.Connection,
    unit_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    machine_identity: MachineIdentity,
) -> None:
    import ava

    calls: list[int] = []
    monkeypatch.setattr(ava, "ensure_plugins_loaded", lambda: calls.append(1))
    sid = _insert_schedule(db_conn, script="exit 0\n", command="bash run.sh")

    assert run(sid, machine=machine_identity) == 0
    assert calls == []


def test_run_record_failure_does_not_break_the_run(
    db_conn: psycopg.Connection,
    unit_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    machine_identity: MachineIdentity,
) -> None:
    import services.wake.schedule_manager.engine as sr

    real_connect = sr.Database.connect

    class _FlakyCursor:
        def __init__(self, cur: Any) -> None:
            self._cur = cur

        def __getattr__(self, name: str) -> Any:
            return getattr(self._cur, name)

        def __enter__(self) -> _FlakyCursor:
            self._cur.__enter__()
            return self

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc_val: BaseException | None,
            exc_tb: Any,
        ) -> None:
            self._cur.__exit__(exc_type, exc_val, exc_tb)

        def execute(self, sql: str, params: Any = None) -> Any:
            if "schedule_runs" in sql:
                raise RuntimeError("db down on schedule_runs")
            return self._cur.execute(sql, params)

    class _Flaky:
        def __init__(self, conn: psycopg.Connection) -> None:
            self._conn = conn

        def __enter__(self) -> _Flaky:
            self._inner = self._conn.__enter__()
            return self

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc_val: BaseException | None,
            exc_tb: Any,
        ) -> None:
            self._conn.__exit__(exc_type, exc_val, exc_tb)

        def cursor(self) -> _FlakyCursor:
            return _FlakyCursor(self._inner.cursor())

        def execute(self, query: Any, params: Any = None) -> Any:
            return self._inner.execute(query, params)

    def _flaky_connect(self: Database, *args: object, **kwargs: object) -> _Flaky:
        return _Flaky(real_connect(self, *args, **kwargs))

    monkeypatch.setattr(sr.Database, "connect", _flaky_connect)
    sid = _insert_schedule(db_conn, script="x = 1\n")

    assert run(sid, machine=machine_identity) == 0
    status, _ = _status_and_error(db_conn, sid)
    assert status == "completed"


def test_run_completed_marker_failure_keeps_run_row_honest(
    db_conn: psycopg.Connection,
    unit_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    machine_identity: MachineIdentity,
) -> None:
    import services.wake.schedule_manager.engine as sr

    real_connect = sr.Database.connect

    class _FlakyCursor:
        def __init__(self, cur: Any) -> None:
            self._cur = cur

        def __getattr__(self, name: str) -> Any:
            return getattr(self._cur, name)

        def __enter__(self) -> _FlakyCursor:
            self._cur.__enter__()
            return self

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc_val: BaseException | None,
            exc_tb: Any,
        ) -> None:
            self._cur.__exit__(exc_type, exc_val, exc_tb)

        def execute(self, sql: str, params: Any = None) -> Any:
            if "status = 'completed'" in sql:
                raise RuntimeError("db down on completed marker")
            return self._cur.execute(sql, params)

    class _Flaky:
        def __init__(self, conn: psycopg.Connection) -> None:
            self._conn = conn

        def __enter__(self) -> _Flaky:
            self._inner = self._conn.__enter__()
            return self

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc_val: BaseException | None,
            exc_tb: Any,
        ) -> None:
            self._conn.__exit__(exc_type, exc_val, exc_tb)

        def cursor(self) -> _FlakyCursor:
            return _FlakyCursor(self._inner.cursor())

        def execute(self, query: Any, params: Any = None) -> Any:
            return self._inner.execute(query, params)

    def _flaky_connect(self: Database, *args: object, **kwargs: object) -> _Flaky:
        return _Flaky(real_connect(self, *args, **kwargs))

    monkeypatch.setattr(sr.Database, "connect", _flaky_connect)
    sid = _insert_schedule(db_conn, script="x = 1\n")

    assert run(sid, machine=machine_identity) == 0
    status, _ = _status_and_error(db_conn, sid)
    assert status != "completed"
    assert _runs(db_conn, sid) == [(True, "completed-marker write failed")]


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("python schedule.py", "schedule.py"),
        ("python3 -u run_me.py", "run_me.py"),
        ("bash run.sh", "run.sh"),
        ("some-binary --flag", "schedule.py"),
        ("python3.11 main.py", "main.py"),
        ("python3.12 -u run_me.py", "run_me.py"),
        ("bash -l run.sh", "run.sh"),
        ("some-binary --config=v1.2 run.py", "run.py"),
    ],
)
def test_script_filename(command: str, expected: str) -> None:
    assert script_filename(command) == expected


def test_run_hung_subprocess_times_out_and_records_error(
    db_conn: psycopg.Connection,
    unit_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    machine_identity: MachineIdentity,
) -> None:

    monkeypatch.setattr(settings.gateway, "schedule_stall_timeout_seconds", 2.0)
    sid = _insert_schedule(db_conn, script="sleep 60\n", command="bash run.sh")

    rc = run(sid, machine=machine_identity)

    assert rc == 1
    status, last_error = _status_and_error(db_conn, sid)
    assert status != "completed"
    assert last_error is not None and "did not finish within 2s" in last_error


def test_stall_verdict_closes_run_row(
    db_conn: psycopg.Connection,
    unit_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    database_gate: ProcessDbGate,
) -> None:
    import services.wake.schedule_manager.engine as sr

    sid = _insert_schedule(db_conn, script="x = 1\n")
    run_id = sr.record_run_start(Database.from_settings(gate=database_gate), sid)
    exited: list[int] = []
    monkeypatch.setattr(sr.os, "_exit", exited.append)
    monkeypatch.setattr(sr.base.host.proc, "kill_process_tree", Mock())
    monkeypatch.setattr(sr.faulthandler, "dump_traceback_later", Mock())

    sr.stall_action(Database.from_settings(gate=database_gate), sid, "stalled in foo", run_id)

    assert exited == [1]
    assert _runs(db_conn, sid) == [(False, f"stalled ({sr.schedule_stall_timeout_seconds():.0f}s)")]
