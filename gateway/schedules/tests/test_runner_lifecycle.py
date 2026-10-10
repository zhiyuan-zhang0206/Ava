"""Real watchdog interleavings and original-error collection."""

from __future__ import annotations

import runpy
import sys
import threading
from pathlib import Path
from typing import Any

import psycopg
import pytest

from base.config import settings
from base.db import Database
from gateway.schedules import runner as sr


def _log_text(records: list[dict[str, Any]]) -> str:
    return "\n".join(str(record["message"]) for record in records)


class WorkerFailure(BaseException):
    """An unexpected failure outside the recoverable Exception boundaries."""


def _fast_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.gateway, "schedule_stall_check_interval_seconds", 0.005)
    monkeypatch.setattr(settings.gateway, "schedule_stall_timeout_seconds", 0.001)


def test_close_rejects_a_stall_sample_already_in_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    _fast_guard(monkeypatch)
    sampled = threading.Event()
    release = threading.Event()
    calls = 0
    actions: list[str] = []

    def sample(_guard: sr._StallGuard) -> tuple[str, int, str]:
        nonlocal calls
        calls += 1
        if calls == 2:
            sampled.set()
            assert release.wait(2)
        return "script.py", 1, "blocked"

    def action(_db: Database, _sid: int, msg: str, _rid: int | None) -> None:
        actions.append(msg)

    monkeypatch.setattr(sr._StallGuard, "_sample", sample)
    monkeypatch.setattr(sr, "_stall_action", action)
    guard = sr._start_stall_guard(Database.from_settings(), 1, None)

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


def test_close_collects_an_admitted_stall_action(monkeypatch: pytest.MonkeyPatch) -> None:
    _fast_guard(monkeypatch)
    admitted = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def action(_db: Database, _sid: int, _msg: str, _rid: int | None) -> None:
        admitted.set()
        assert release.wait(2)
        finished.set()

    def sample(_guard: sr._StallGuard) -> tuple[str, int, str]:
        return "script.py", 1, "blocked"

    monkeypatch.setattr(sr._StallGuard, "_sample", sample)
    monkeypatch.setattr(sr, "_stall_action", action)
    guard = sr._start_stall_guard(Database.from_settings(), 1, None)

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
    monkeypatch: pytest.MonkeyPatch, loguru_records: list[dict[str, Any]]
) -> None:
    _fast_guard(monkeypatch)
    failed = threading.Event()
    error = WorkerFailure("frame reader implementation failed")

    def sample(_guard: sr._StallGuard) -> None:
        failed.set()
        raise error

    monkeypatch.setattr(sr._StallGuard, "_sample", sample)
    guard = sr._start_stall_guard(Database.from_settings(), 1, None)
    assert failed.wait(2)
    with pytest.raises(WorkerFailure) as caught:
        guard.close()
    assert caught.value is error
    assert guard.error is error
    assert not guard.thread.is_alive()
    assert "stall guard failed" in _log_text(loguru_records)


def test_late_recorder_error_remains_owned_after_deadline(
    monkeypatch: pytest.MonkeyPatch, loguru_records: list[dict[str, Any]]
) -> None:
    started = threading.Event()
    release = threading.Event()
    error = WorkerFailure("late database implementation failure")
    second_writes: list[int] = []

    def record_error(*_args: object) -> None:
        started.set()
        assert release.wait(2)
        raise error

    monkeypatch.setattr(settings.gateway, "schedule_stall_exit_record_deadline_seconds", 0.02)
    monkeypatch.setattr(sr, "_record_error", record_error)

    def record_end(*_args: object, **_kwargs: object) -> None:
        second_writes.append(1)

    monkeypatch.setattr(sr, "_record_run_end", record_end)
    recorder = sr._StallRecorder(Database.from_settings(), 1, "stall", None)
    try:
        assert started.wait(2)
        assert recorder.close() is False
        assert recorder.thread.is_alive()
        assert "unfinished at hard-exit deadline" in _log_text(loguru_records)
    finally:
        release.set()
        recorder.thread.join(timeout=2)
    assert not recorder.thread.is_alive()
    assert "stall recorder failed" in _log_text(loguru_records)
    with pytest.raises(WorkerFailure) as caught:
        recorder.close()
    assert caught.value is error
    assert recorder.error is error
    assert second_writes == []


def test_recorder_does_not_start_second_write_after_close(monkeypatch: pytest.MonkeyPatch) -> None:
    started = threading.Event()
    release = threading.Event()
    second_writes: list[int] = []

    def record_error(*_args: object) -> None:
        started.set()
        assert release.wait(2)

    monkeypatch.setattr(settings.gateway, "schedule_stall_exit_record_deadline_seconds", 0.02)
    monkeypatch.setattr(sr, "_record_error", record_error)

    def record_end(*_args: object, **_kwargs: object) -> None:
        second_writes.append(1)

    monkeypatch.setattr(sr, "_record_run_end", record_end)
    recorder = sr._StallRecorder(Database.from_settings(), 1, "stall", None)
    try:
        assert started.wait(2)
        assert recorder.close() is False
    finally:
        release.set()
        recorder.thread.join(timeout=2)
    assert recorder.close() is True
    assert second_writes == []


@pytest.mark.parametrize(
    "primary", [ValueError("script failed"), SystemExit(3), SystemExit(True), SystemExit("failed")]
)
def test_script_failure_stays_primary_when_guard_close_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    loguru_records: list[dict[str, Any]],
    primary: BaseException,
) -> None:
    _fast_guard(monkeypatch)
    failed = threading.Event()
    secondary = WorkerFailure("guard failed")
    guards: list[sr._StallGuard] = []
    original_start = sr._start_stall_guard
    argv = sys.argv
    sleep = sr.time.sleep

    def sample(_guard: sr._StallGuard) -> None:
        failed.set()
        raise secondary

    def start(db: Database, sid: int, rid: int | None) -> sr._StallGuard:
        guard = original_start(db, sid, rid)
        guards.append(guard)
        return guard

    def script(*_args: object, **_kwargs: object) -> None:
        assert failed.wait(2)
        raise primary

    monkeypatch.setattr(sr._StallGuard, "_sample", sample)
    monkeypatch.setattr(sr, "_start_stall_guard", start)
    monkeypatch.setattr(runpy, "run_path", script)
    import ava

    monkeypatch.setattr(ava, "ensure_plugins_loaded", lambda: None)
    with pytest.raises(type(primary)) as caught:
        sr._run_python_script(Database.from_settings(), 1, None, tmp_path / "script.py")
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
    monkeypatch: pytest.MonkeyPatch, loguru_records: list[dict[str, Any]]
) -> None:
    _fast_guard(monkeypatch)
    started = threading.Event()
    release = threading.Event()
    error = WorkerFailure("late observer failure")

    def sample(_guard: sr._StallGuard) -> None:
        started.set()
        assert release.wait(2)
        raise error

    monkeypatch.setattr(sr._StallGuard, "_sample", sample)
    guard = sr._start_stall_guard(Database.from_settings(), 1, None)
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
    monkeypatch: pytest.MonkeyPatch, loguru_records: list[dict[str, Any]]
) -> None:
    error = WorkerFailure("cleanup implementation failed")
    exits: list[int] = []

    def cleanup(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr(sr.base.host.proc, "kill_process_tree", cleanup)
    monkeypatch.setattr(sr.os, "_exit", exits.append)
    with pytest.raises(WorkerFailure) as caught:
        sr._stall_action(Database.from_settings(), 1, "stall", None)
    assert caught.value is error
    assert exits == [1]
    assert "stall action failed" in _log_text(loguru_records)


@pytest.mark.parametrize("exit_code", [0, None, False])
@pytest.mark.parametrize("blocked", [False, True], ids=["unknown-error", "blocked-close"])
def test_successful_script_exit_cannot_complete_after_guard_close_failure(
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection,
    unit_home: Path,
    exit_code: int | None,
    blocked: bool,
    loguru_records: list[dict[str, Any]],
) -> None:
    _fast_guard(monkeypatch)
    failed = threading.Event()
    release = threading.Event()
    error = WorkerFailure("original unknown guard failure")
    guards: list[sr._StallGuard] = []
    original_start = sr._start_stall_guard

    def sample(_guard: sr._StallGuard) -> None:
        failed.set()
        if blocked:
            assert release.wait(3)
        raise error

    def start(db: Database, sid: int, rid: int | None) -> sr._StallGuard:
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
    monkeypatch.setattr(sr._StallGuard, "_sample", sample)
    monkeypatch.setattr(sr, "_start_stall_guard", start)
    monkeypatch.setattr(runpy, "run_path", script)
    import ava

    monkeypatch.setattr(ava, "ensure_plugins_loaded", lambda: None)
    try:
        if blocked:
            assert sr.run(row[0]) == 1
            assert guards[0].thread.is_alive()
            assert "stall guard did not stop" in _log_text(loguru_records)
        else:
            with pytest.raises(WorkerFailure) as caught:
                sr.run(row[0])
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
