"""Tests for gateway/schedule_runner.py — the session schedule entrypoint.

Exercises the runner mechanics against the real test DB + a per-test $AVA_HOME:
materialize the script, bind the schedule actor, run it, capture a crash. The
reuse-ladder behavior of a real generated script (spawn/resurrect/wake) is NOT
tested here — that needs a live gateway and is covered end-to-end elsewhere.
"""

from __future__ import annotations

import sys
from pathlib import Path

import psycopg
import pytest

import ava._boot as boot
from gateway.schedule_runner import _script_filename, run


@pytest.fixture(autouse=True)
def _restore_actor(monkeypatch: pytest.MonkeyPatch) -> None:
    # run() calls establish_actor + sets AVA_SCHEDULE_ID; keep both out of other tests.
    monkeypatch.setattr(boot, "_actor", boot._actor)
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
    db_conn: psycopg.Connection, unit_home: Path
) -> None:
    marker = unit_home / "ran.txt"
    # The script records the actor it runs under — proving establish_actor fired
    # before the script executed.
    script = f"import ava._boot, pathlib\npathlib.Path({str(marker)!r}).write_text(ava._boot.require_actor())\n"
    sid = _insert_schedule(db_conn, script=script)

    rc = run(sid)

    assert rc == 0
    assert (unit_home / "schedules" / str(sid) / "schedule.py").read_text() == script
    assert marker.read_text() == f"schedule:{sid}"


def test_run_marks_completed_on_clean_py_exit(db_conn: psycopg.Connection, unit_home: Path) -> None:
    # A .py script that returns (rc=0) is a deliberate finish, not a crash.
    sid = _insert_schedule(db_conn, script="x = 1 + 1\n")

    rc = run(sid)

    assert rc == 0
    status, last_error = _status_and_error(db_conn, sid)
    assert status == "completed"
    assert last_error is None


def test_run_marks_completed_on_clean_subprocess_exit(
    db_conn: psycopg.Connection, unit_home: Path
) -> None:
    sid = _insert_schedule(db_conn, script="exit 0\n", command="bash run.sh")

    rc = run(sid)

    assert rc == 0
    status, _ = _status_and_error(db_conn, sid)
    assert status == "completed"


def test_run_captures_crash_to_last_error(db_conn: psycopg.Connection, unit_home: Path) -> None:
    sid = _insert_schedule(db_conn, script="raise RuntimeError('boom-42')\n")

    rc = run(sid)

    assert rc == 1
    status, last_error = _status_and_error(db_conn, sid)
    # A crash records the traceback and does NOT mark the schedule completed —
    # the manager owns the relaunch/breaker decision on `status`.
    assert last_error is not None and "boom-42" in last_error
    assert status != "completed"


def test_run_nonzero_subprocess_is_not_completed(
    db_conn: psycopg.Connection, unit_home: Path
) -> None:
    sid = _insert_schedule(db_conn, script="exit 3\n", command="bash run.sh")

    rc = run(sid)

    assert rc == 3
    status, last_error = _status_and_error(db_conn, sid)
    assert status != "completed"
    assert last_error is not None and "exited 3" in last_error


def test_run_skips_disabled_schedule(db_conn: psycopg.Connection, unit_home: Path) -> None:
    sid = _insert_schedule(db_conn, script="raise RuntimeError('should not run')\n", enabled=False)
    assert run(sid) == 0
    # Nothing materialized, no error recorded.
    assert not (unit_home / "schedules" / str(sid)).exists()
    with db_conn.cursor() as cur:
        cur.execute("SELECT last_error FROM schedules WHERE id = %s", (sid,))
        row = cur.fetchone()
    assert row is not None and row[0] is None


def test_run_loads_plugins_for_py_script(
    db_conn: psycopg.Connection, unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The in-process .py path must load plugin namespaces first — this runner never
    # builds the agent graph, so nothing else does, and a schedule that touches
    # ava.tasks would otherwise hit the factory `import ava` and AttributeError.
    import ava

    calls: list[int] = []
    monkeypatch.setattr(ava, "_ensure_plugins_loaded", lambda: calls.append(1))
    sid = _insert_schedule(db_conn, script="x = 1\n")

    assert run(sid) == 0
    assert calls == [1]


def test_run_skips_plugin_load_for_non_py_command(
    db_conn: psycopg.Connection, unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A non-.py shell command (bash etc.) runs in its own subprocess and does not
    # import ava, so this runner's process does not load plugins for it.
    import ava

    calls: list[int] = []
    monkeypatch.setattr(ava, "_ensure_plugins_loaded", lambda: calls.append(1))
    sid = _insert_schedule(db_conn, script="exit 0\n", command="bash run.sh")

    assert run(sid) == 0
    assert calls == []


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
    assert _script_filename(command) == expected


# ── stall guard (2026-08-03: a hung schedule sat "alive" through the gateway
#    freeze window and silently missed its Monday fire — the guard hard-exits
#    so the manager's crash path gets a chance) ─────────────────────────────


def test_stall_guard_fires_on_a_stalled_main_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A main-thread frame that does not advance past the timeout trips the
    guard (the stall verdict is recorded; the hard exit itself is os._exit in
    the guard thread and is not exercised in-process)."""
    import time

    import gateway.schedule_runner as sr

    monkeypatch.setattr(sr, "_STALL_CHECK_INTERVAL_S", 0.02)
    monkeypatch.setattr(sr, "_STALL_TIMEOUT_S", 0.1)
    fired: list[str] = []
    monkeypatch.setattr(sr, "_stall_action", lambda _sid, msg: fired.append(msg))  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]

    stop = sr._start_stall_guard(1)
    try:
        deadline = time.monotonic() + 2.0
        while not fired and time.monotonic() < deadline:
            x = 0
            x += 1  # a stable, non-park frame for the guard to observe
        assert fired, "stall guard never fired on a stalled main thread"
        assert "stalled" in fired[0]
    finally:
        stop.set()


def test_stall_guard_ignores_a_legitimately_sleeping_main_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resident schedule parks in time.sleep between fire windows — that is
    the design, not a stall, so the guard must stay quiet."""
    import time

    import gateway.schedule_runner as sr

    monkeypatch.setattr(sr, "_STALL_CHECK_INTERVAL_S", 0.02)
    monkeypatch.setattr(sr, "_STALL_TIMEOUT_S", 0.05)
    fired: list[str] = []
    monkeypatch.setattr(sr, "_stall_action", lambda _sid, msg: fired.append(msg))  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]

    # The runner wraps time.sleep before running a script; without the wrapper
    # the deepest Python frame of a sleeping main thread is sleep's *caller*,
    # which is indistinguishable from a stall.
    _real_sleep = time.sleep
    sr._patch_park_detection()
    stop = sr._start_stall_guard(1)
    try:
        time.sleep(0.4)  # main thread parked in time.sleep the whole time
        assert fired == []
    finally:
        stop.set()
        # The wrapper is process-wide (`time.sleep = sleep` in
        # schedule_runner._patch_park_detection) — restore it or every later
        # `assert time.sleep is _REAL_SLEEP` guard (test_start_readiness_gate)
        # in this worker session fails on the wrapper.
        time.sleep = _real_sleep


def test_run_hung_subprocess_times_out_and_records_error(
    db_conn: psycopg.Connection, unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-.py command that never exits must be bounded: the runner records
    last_error and returns non-zero so the ScheduleManager's crash path
    (backoff + breaker) relaunches instead of the schedule silently eating
    every future fire window (2026-08-08 audit P2-2)."""
    import gateway.schedule_runner as sr

    monkeypatch.setattr(sr, "_STALL_TIMEOUT_S", 2.0)
    sid = _insert_schedule(db_conn, script="sleep 60\n", command="bash run.sh")

    rc = run(sid)

    assert rc == 1
    status, last_error = _status_and_error(db_conn, sid)
    assert status != "completed"
    assert last_error is not None and "did not finish within 2s" in last_error


def test_main_refuses_on_foreign_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    """issue #194: the runner refuses to start from a worktree-anchored checkout."""
    import gateway.schedule_runner as runner

    monkeypatch.setattr(sys, "argv", ["schedule_runner", "1"])
    monkeypatch.setattr(runner, "prod_service_checkout_error", _refuse_foreign_checkout)
    with pytest.raises(SystemExit) as excinfo:
        runner.main()
    assert excinfo.value.code == 3


def _refuse_foreign_checkout(_repo: Path) -> str:
    return "prod home but foreign checkout"
