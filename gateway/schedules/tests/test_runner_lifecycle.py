"""Real watchdog interleavings and original-error collection."""

from __future__ import annotations

import runpy
import sys
import threading
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import psycopg
import pytest

from ava.sdk_surface import process_context
from base.cluster.machine import MachineIdentity
from base.config import settings
from base.daemon.schedules.inputs import ScheduleInputs
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from gateway.schedules.tests.runner_inputs import run_schedule as run
from services.wake.schedule_manager import engine as sr
from tests.fixtures.unit.identity import machine_identity as machine_identity


def _log_text(records: list[dict[str, Any]]) -> str:
    return "\n".join(str(record["message"]) for record in records)


class WorkerFailure(BaseException):
    """An unexpected failure outside the recoverable Exception boundaries."""


def _fast_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.gateway, "schedule_stall_timeout_seconds", 0.02)


def test_close_rejects_a_stall_sample_already_in_progress(
    monkeypatch: pytest.MonkeyPatch, database_gate: ProcessDbGate
) -> None:
    _fast_guard(monkeypatch)
    sampled = threading.Event()
    release = threading.Event()
    calls = 0
    actions: list[str] = []

    def sample(_guard: sr.StallGuard) -> tuple[str, int, str]:
        nonlocal calls
        calls += 1
        if calls == 2:
            sampled.set()
            assert release.wait(2)
        return "script.py", 1, "blocked"

    def action(_db: Database, _sid: int, msg: str, _rid: int | None) -> None:
        actions.append(msg)

    monkeypatch.setattr(sr.StallGuard, "_sample", sample)
    monkeypatch.setattr(sr, "stall_action", action)
    guard = sr.StallGuard(Database.from_settings(gate=database_gate), 1, None)

    def release_on_stop() -> None:
        assert guard.stop.wait(2)
        release.set()

    wake = threading.Thread(target=release_on_stop)
    try:
        assert sampled.wait(2)
        wake.start()
        guard.close()
        assert guard.stop.is_set()
        assert not guard.thread.is_alive()
        assert not guard.action_started
        assert actions == []
    finally:
        release.set()
        if wake.ident is not None:
            wake.join(timeout=2)
        guard.close()


def test_close_collects_an_admitted_stall_action(
    monkeypatch: pytest.MonkeyPatch, database_gate: ProcessDbGate
) -> None:
    _fast_guard(monkeypatch)
    admitted = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def action(_db: Database, _sid: int, _msg: str, _rid: int | None) -> None:
        admitted.set()
        assert release.wait(2)
        finished.set()

    def sample(_guard: sr.StallGuard) -> tuple[str, int, str]:
        return "script.py", 1, "blocked"

    monkeypatch.setattr(sr.StallGuard, "_sample", sample)
    monkeypatch.setattr(sr, "stall_action", action)
    guard = sr.StallGuard(Database.from_settings(gate=database_gate), 1, None)

    def release_on_stop() -> None:
        assert guard.stop.wait(2)
        release.set()

    wake = threading.Thread(target=release_on_stop)
    try:
        assert admitted.wait(2)
        wake.start()
        guard.close()
        assert guard.action_started
        assert finished.is_set()
        assert not guard.thread.is_alive()
    finally:
        release.set()
        if wake.ident is not None:
            wake.join(timeout=2)
        guard.close()


def test_guard_unknown_error_is_visible_and_rethrown(
    monkeypatch: pytest.MonkeyPatch,
    loguru_records: list[dict[str, Any]],
    database_gate: ProcessDbGate,
) -> None:
    _fast_guard(monkeypatch)
    failed = threading.Event()
    error = WorkerFailure("frame reader implementation failed")

    def sample(_guard: sr.StallGuard) -> None:
        failed.set()
        raise error

    monkeypatch.setattr(sr.StallGuard, "_sample", sample)
    guard = sr.StallGuard(Database.from_settings(gate=database_gate), 1, None)
    assert failed.wait(2)
    with pytest.raises(WorkerFailure) as caught:
        guard.close()
    assert caught.value is error
    assert guard.error is error
    assert not guard.thread.is_alive()
    assert "stall guard failed" in _log_text(loguru_records)


@pytest.mark.parametrize(
    "primary", [ValueError("script failed"), SystemExit(3), SystemExit(True), SystemExit("failed")]
)
def test_script_failure_stays_primary_when_guard_close_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    loguru_records: list[dict[str, Any]],
    primary: BaseException,
    database_gate: ProcessDbGate,
    *,
    machine_identity: MachineIdentity,
) -> None:
    _fast_guard(monkeypatch)
    failed = threading.Event()
    secondary = WorkerFailure("guard failed")
    guards: list[sr.StallGuard] = []
    original_start = sr.StallGuard  # captured before the factory replaces it
    argv = sys.argv
    sleep = sr.time.sleep

    def sample(_guard: sr.StallGuard) -> None:
        failed.set()
        raise secondary

    def start(db: Database, sid: int, rid: int | None) -> sr.StallGuard:
        guard = original_start(db, sid, rid)
        guards.append(guard)
        return guard

    def script(*_args: object, **_kwargs: object) -> None:
        assert failed.wait(2)
        raise primary

    monkeypatch.setattr(sr.StallGuard, "_sample", sample)
    monkeypatch.setattr(sr, "StallGuard", start)
    monkeypatch.setattr(runpy, "run_path", script)
    import ava

    monkeypatch.setattr(ava, "ensure_plugins_loaded", lambda: None)
    with pytest.raises(type(primary)) as caught:
        sr.run_python_script(
            Database.from_settings(gate=database_gate),
            1,
            None,
            tmp_path / "script.py",
            load_plugins=ava.ensure_plugins_loaded,
            inputs=ScheduleInputs(
                lambda: Database.from_settings(gate=database_gate),
                Mock(),
                process_context.loaded_code_image(),
                machine=machine_identity,
            ),
        )
    assert caught.value is primary
    assert sys.argv is argv
    assert sr.time.sleep is sleep
    assert "stall guard failed" in _log_text(loguru_records)
    assert "guard close failed" in _log_text(loguru_records)
    with pytest.raises(WorkerFailure) as caught:
        guards[0].close()
    assert caught.value is secondary
    assert not guards[0].thread.is_alive()


def test_blocked_guard_close_is_finite_and_collects_its_late_error(
    monkeypatch: pytest.MonkeyPatch,
    loguru_records: list[dict[str, Any]],
    database_gate: ProcessDbGate,
) -> None:
    _fast_guard(monkeypatch)
    started = threading.Event()
    release = threading.Event()
    error = WorkerFailure("late observer failure")

    def sample(_guard: sr.StallGuard) -> None:
        started.set()
        assert release.wait(2)
        raise error

    monkeypatch.setattr(sr.StallGuard, "_sample", sample)
    guard = sr.StallGuard(Database.from_settings(gate=database_gate), 1, None)
    try:
        assert started.wait(2)
        with pytest.raises(RuntimeError, match="stall guard did not stop"):
            guard.close(timeout=0.01)
        assert guard.thread.is_alive()
    finally:
        release.set()
        guard.thread.join(timeout=2)
    assert "stall guard failed" in _log_text(loguru_records)
    with pytest.raises(WorkerFailure) as caught:
        guard.close()
    assert caught.value is error
    assert not guard.thread.is_alive()


def test_stall_action_keeps_hard_exit_when_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch, database_gate: ProcessDbGate
) -> None:
    error = WorkerFailure("cleanup implementation failed")
    exits: list[int] = []

    def cleanup(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr(sr.base.host.proc, "kill_process_tree", cleanup)
    monkeypatch.setattr(sr.os, "_exit", exits.append)
    with pytest.raises(WorkerFailure) as caught:
        sr.stall_action(Database.from_settings(gate=database_gate), 1, "stall", None)
    assert caught.value is error
    assert exits == [1]


def test_stall_action_arms_one_deadline_before_the_failure_writes(
    monkeypatch: pytest.MonkeyPatch, database_gate: ProcessDbGate
) -> None:
    """The interpreter-level deadline (dump stacks, exit 1) is armed before any DB write."""
    order: list[str] = []
    armed: list[tuple[float, bool]] = []

    def arm(timeout: float, *, exit: bool) -> None:
        order.append("arm")
        armed.append((timeout, exit))

    def record_error(*_args: object) -> None:
        order.append("error")

    def record_end(*_args: object, **_kwargs: object) -> None:
        order.append("run")

    monkeypatch.setattr(sr.faulthandler, "dump_traceback_later", arm)
    monkeypatch.setattr(sr.base.host.proc, "kill_process_tree", Mock())
    monkeypatch.setattr(sr.os, "_exit", Mock())
    monkeypatch.setattr(sr, "record_error", record_error)
    monkeypatch.setattr(sr, "record_run_end", record_end)
    sr.stall_action(Database.from_settings(gate=database_gate), 1, "stall", None)
    assert order == ["arm", "error", "run"]
    assert armed == [(sr.FAILURE_RECORD_DEADLINE_S, True)]


def test_frame_read_failure_is_a_guard_failure_not_a_skipped_sample(
    monkeypatch: pytest.MonkeyPatch,
    loguru_records: list[dict[str, Any]],
    database_gate: ProcessDbGate,
) -> None:
    """The frame reader has no recovery branch: any failure ends the guard and is raised at close."""
    _fast_guard(monkeypatch)
    failed = threading.Event()
    error = RuntimeError("frame reader failed")

    def sample(_guard: sr.StallGuard) -> None:
        failed.set()
        raise error

    monkeypatch.setattr(sr.StallGuard, "_sample", sample)
    guard = sr.StallGuard(Database.from_settings(gate=database_gate), 1, None)
    assert failed.wait(2)
    with pytest.raises(RuntimeError) as caught:
        guard.close()
    assert caught.value is error
    assert not guard.thread.is_alive()
    assert "stall guard failed" in _log_text(loguru_records)


@pytest.mark.parametrize("exit_code", [0, None, False])
@pytest.mark.parametrize("blocked", [False, True], ids=["unknown-error", "blocked-close"])
def test_successful_script_exit_cannot_complete_after_guard_close_failure(
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection,
    unit_home: Path,
    exit_code: int | None,
    blocked: bool,
    loguru_records: list[dict[str, Any]],
    *,
    machine_identity: MachineIdentity,
) -> None:
    _fast_guard(monkeypatch)
    failed = threading.Event()
    release = threading.Event()
    error = WorkerFailure("original unknown guard failure")
    guards: list[sr.StallGuard] = []
    original_start = sr.StallGuard  # captured before the factory replaces it

    def sample(_guard: sr.StallGuard) -> None:
        failed.set()
        if blocked:
            assert release.wait(3)
        raise error

    def start(db: Database, sid: int, rid: int | None) -> sr.StallGuard:
        guard = original_start(db, sid, rid)
        guards.append(guard)
        return guard

    def script(*_args: object, **_kwargs: object) -> None:
        assert failed.wait(2)
        raise SystemExit(exit_code)

    row = db_conn.execute(
        "INSERT INTO schedules (name, script, command, enabled, status) "
        "VALUES ('exit-cleanup', 'pass', 'python script.py', true, 'stopped') RETURNING id"
    ).fetchone()
    db_conn.commit()
    assert row is not None
    monkeypatch.setattr(sr.StallGuard, "_sample", sample)
    monkeypatch.setattr(sr, "StallGuard", start)
    monkeypatch.setattr(runpy, "run_path", script)
    import ava

    monkeypatch.setattr(ava, "ensure_plugins_loaded", lambda: None)
    try:
        if blocked:
            assert run(row[0], machine=machine_identity) == 1
            assert guards[0].thread.is_alive()
            assert "stall guard did not stop" in _log_text(loguru_records)
        else:
            with pytest.raises(WorkerFailure) as caught:
                run(row[0], machine=machine_identity)
            assert caught.value is error
        assert db_conn.execute(
            "SELECT status FROM schedules WHERE id = %s", (row[0],)
        ).fetchone() == ("stopped",)
        outcome = db_conn.execute(
            "SELECT ok FROM schedule_runs WHERE schedule_id = %s", (row[0],)
        ).fetchone()
        assert outcome == ((False,) if blocked else (None,))
    finally:
        release.set()
        for guard in guards:
            guard.thread.join(timeout=2)
    assert "stall guard failed" in _log_text(loguru_records)
    with pytest.raises(WorkerFailure) as caught:
        guards[0].close()
    assert caught.value is error
    assert guards[0].error is error
    assert not guards[0].thread.is_alive()
