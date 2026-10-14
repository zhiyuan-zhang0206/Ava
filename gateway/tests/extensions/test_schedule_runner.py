"""Tests for services/wake/schedule_manager/engine.py — the schedule execution engine.

Exercises the runner mechanics against the real test DB + a per-test $AVA_HOME:
materialize the script, bind the schedule actor, run it, capture a crash. The
reuse-ladder behavior of a real generated script (spawn/resurrect/wake) is NOT
tested here — that needs a live gateway and is covered end-to-end elsewhere.
"""

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
    # run() binds a schedule-actor context (restored by the identity fixture) and sets AVA_SCHEDULE_ID;
    # keep the latter out of other tests.
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
    # The script records the actor it runs under — proving establish_actor fired
    # before the script executed.
    script = f"import ava.sdk_surface.agent_identity, pathlib\npathlib.Path({str(marker)!r}).write_text(ava.sdk_surface.agent_identity.require_actor())\n"
    sid = _insert_schedule(db_conn, script=script)

    rc = run(sid, machine=machine_identity)

    assert rc == 0
    assert (unit_home / "schedules" / str(sid) / "schedule.py").read_text() == script
    assert marker.read_text() == f"schedule:{sid}"


def test_run_marks_completed_on_clean_py_exit(
    db_conn: psycopg.Connection, unit_home: Path, *, machine_identity: MachineIdentity
) -> None:
    # A .py script that returns (rc=0) is a deliberate finish, not a crash.
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
    # A crash records the traceback and does NOT mark the schedule completed —
    # the manager owns the relaunch/breaker decision on `status`.
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
    """(ok, note) rows for a schedule, oldest first."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ok, note FROM schedule_runs WHERE schedule_id = %s ORDER BY id",
            (sid,),
        )
        return [(r[0], r[1]) for r in cur.fetchall()]


def test_run_records_clean_py_exit(
    db_conn: psycopg.Connection, unit_home: Path, *, machine_identity: MachineIdentity
) -> None:
    # A clean .py exit closes its run-history row with ok=true.
    sid = _insert_schedule(db_conn, script="x = 1 + 1\n")

    assert run(sid, machine=machine_identity) == 0
    assert _runs(db_conn, sid) == [(True, None)]


def test_run_records_crash(
    db_conn: psycopg.Connection, unit_home: Path, *, machine_identity: MachineIdentity
) -> None:
    sid = _insert_schedule(db_conn, script="raise RuntimeError('x' * 5000 + 'the-end')\n")

    assert run(sid, machine=machine_identity) == 1
    [(ok, note)] = _runs(db_conn, sid)
    assert ok is False and note is not None and len(note) <= 3100  # the traceback's tail only
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
    # A .py script that calls sys.exit(0) exits deliberately — a finish like a
    # clean return, not a crash and not an eternal in-progress row (QA P3-3).
    sid = _insert_schedule(db_conn, script="raise SystemExit(0)\n")

    assert run(sid, machine=machine_identity) == 0
    status, last_error = _status_and_error(db_conn, sid)
    assert status == "completed"
    assert last_error is None
    assert _runs(db_conn, sid) == [(True, None)]


def test_run_records_py_sys_exit_nonzero(
    db_conn: psycopg.Connection, unit_home: Path, *, machine_identity: MachineIdentity
) -> None:
    # A .py script that calls sys.exit(3) is recorded like a command's nonzero
    # exit — the row closes ok=false and the manager's crash path relaunches.
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
        # A non-int code is a message the interpreter would print to stderr —
        # it rides the run note instead of being swallowed (QA N1).
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
        # bool is an int subclass — the interpreter treats it as an exit code:
        # True -> 1 (failure), False -> 0 (clean finish). int() normalization
        # keeps the note wording honest ("script exited 1", not "True").
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
    # Nothing materialized, no error recorded.
    assert not (unit_home / "schedules" / str(sid)).exists()
    with db_conn.cursor() as cur:
        cur.execute("SELECT last_error FROM schedules WHERE id = %s", (sid,))
        row = cur.fetchone()
    assert row is not None and row[0] is None
    # A schedule that never ran has no run-history rows either.
    assert _runs(db_conn, sid) == []


def test_run_loads_plugins_for_py_script(
    db_conn: psycopg.Connection,
    unit_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    machine_identity: MachineIdentity,
) -> None:
    # The in-process .py path must load plugin namespaces first — this runner never
    # builds the agent graph, so nothing else does, and a schedule that touches
    # ava.tasks would otherwise hit the factory `import ava` and AttributeError.
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
    # The gateway launches the runner as `python -m services.wake.schedule_manager.runner <id>`,
    # so this process's sys.argv carries the schedule id. A .py script runs
    # in-process and must see the argv `python <script>` would give it — just its
    # own path — not the runner's (2026-09-22: the daily debt sweep's argparse
    # rejected the leaked id and exited 2 on every launch).
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
    # The runner keeps its own argv for whatever follows the script.
    assert sys.argv == ["schedule_runner", "4242"]


def test_run_skips_plugin_load_for_non_py_command(
    db_conn: psycopg.Connection,
    unit_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    machine_identity: MachineIdentity,
) -> None:
    # A non-.py shell command (bash etc.) runs in its own subprocess and does not
    # import ava, so this runner's process does not load plugins for it.
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
    # Run history is severable observability: a DB failure on the run-record
    # writes must not affect the schedule itself (clean exit still marks
    # completed, rc still 0). Only the schedule_runs statements fail — the
    # schedules-row writes (status/last_error) keep working.
    import services.wake.schedule_manager.engine as sr

    real_connect = sr.Database.connect

    class _FlakyCursor:
        """Cursor proxy that fails every schedule_runs statement."""

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
        """Connection proxy whose cursor fails every schedule_runs statement."""

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
    # QA P3-5: a failure writing the 'completed' status marker must not record
    # the run as 'crashed: OperationalError' — the script finished fine, only
    # the bookkeeping lost a write. The run row reads ok=true with a note;
    # the manager still relaunches (status is not 'completed'), which is the
    # same safe side as before, just without a false crash record.
    import services.wake.schedule_manager.engine as sr

    real_connect = sr.Database.connect

    class _FlakyCursor:
        """Cursor proxy that fails only the completed-marker statement."""

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
        """Connection proxy whose cursor fails the completed-marker statement."""

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
    assert status != "completed"  # the marker write failed — manager will relaunch
    assert _runs(db_conn, sid) == [(True, "completed-marker write failed")]


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("python schedule.py", "schedule.py"),
        ("python3 -u run_me.py", "run_me.py"),
        ("bash run.sh", "run.sh"),
        ("some-binary --flag", "schedule.py"),  # no filename token -> default
        # A versioned interpreter must not be mistaken for the script file
        # (audit P2-8): python3.11 has a dotted name but is not a script.
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
    """A non-.py command that never exits must be bounded: the runner records
    last_error and returns non-zero so the ScheduleManager's crash path
    (backoff + breaker) relaunches instead of the schedule silently eating
    every future fire window (2026-08-08 audit P2-2)."""

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
    # QA P2-1: a stall closes its run row as failed instead of forever in-progress.
    import services.wake.schedule_manager.engine as sr

    sid = _insert_schedule(db_conn, script="x = 1\n")
    run_id = sr.record_run_start(Database.from_settings(gate=database_gate), sid)
    exited: list[int] = []
    monkeypatch.setattr(sr.os, "_exit", exited.append)
    monkeypatch.setattr(sr.base.host.proc, "kill_process_tree", Mock())
    # The deadline timer would exit this test process for real; its contract is
    # covered by the lifecycle test and the real-process integration test.
    monkeypatch.setattr(sr.faulthandler, "dump_traceback_later", Mock())

    sr.stall_action(Database.from_settings(gate=database_gate), sid, "stalled in foo", run_id)

    assert exited == [1]
    assert _runs(db_conn, sid) == [(False, f"stalled ({sr.schedule_stall_timeout_seconds():.0f}s)")]
