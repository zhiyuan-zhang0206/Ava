from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from base.deploy.lifecycle import status_journal as journal


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("MEIVA_HOME", str(tmp_path))
    return tmp_path


def test_begin_phase_finish_round_trip(home: Path) -> None:
    assert journal.begin("stop", deadline=time.monotonic() + 30) is True
    with journal.phase("drain"):
        pass
    journal.finish(0)
    op = journal.read()
    assert op is not None
    assert op.operation == "stop"
    assert op.complete is True
    assert op.result == {"rc": 0, "error": None}
    assert [p.name for p in op.phases] == ["drain"]
    assert op.phases[0].ok is True
    assert op.phases[0].finished_at is not None


def test_failed_phase_is_recorded_and_reraised(home: Path) -> None:
    journal.begin("stop")
    with pytest.raises(RuntimeError, match="boom"), journal.phase("services"):
        raise RuntimeError("boom")
    journal.finish(1, error="RuntimeError: boom", extra={"phases": "services 1.2s"})
    op = journal.read()
    assert op is not None
    assert op.complete is True
    assert op.phases[0].ok is False
    assert op.phases[0].finished_at is not None
    assert op.result is not None and op.result["rc"] == 1
    assert "boom" in op.result["error"]


def test_inner_stop_leg_never_hijacks_the_restart_journal(home: Path) -> None:
    assert journal.begin("restart") is True
    with journal.phase("preflight"):
        pass
    assert journal.begin("stop") is False
    with journal.phase("stop"), journal.phase("drain"):
        pass
    op = journal.read()
    assert op is not None
    assert op.operation == "restart"
    assert op.complete is False
    assert [p.name for p in op.phases] == ["preflight", "stop", "drain"]


def test_completed_journal_can_be_replaced(home: Path) -> None:
    journal.begin("stop")
    journal.finish(0)
    assert journal.begin("restart") is True
    op = journal.read()
    assert op is not None and op.operation == "restart" and op.complete is False


def test_malformed_journal_reads_as_absent(home: Path) -> None:
    path = journal.status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    assert journal.read() is None
    path.write_text(json.dumps({"operation": 5}))
    assert journal.read() is None


def test_wrong_shape_journal_reads_as_absent(home: Path) -> None:
    path = journal.status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([{"operation": "stop"}]))
    assert journal.read() is None
    path.write_text(json.dumps({"operation": "stop"}))
    assert journal.read() is None
    path.write_text(json.dumps({"operation": "stop", "pid": "x"}))
    assert journal.read() is None
    path.write_text(json.dumps({"operation": "stop", "pid": 1, "started_at": []}))
    assert journal.read() is None
    path.write_text(json.dumps({"operation": "stop", "pid": 1, "deadline": "soon"}))
    assert journal.read() is None


def test_journal_survives_an_unfinished_process(home: Path) -> None:
    journal.begin("restart")
    with journal.phase("stop"):
        pass
    with pytest.raises(SystemExit), journal.phase("start"):
        raise SystemExit(1)
    op = journal.read()
    assert op is not None
    assert op.complete is False
    assert op.phases[-1].name == "start"
    assert op.phases[-1].ok is False


def test_phase_without_begin_still_journals(home: Path) -> None:
    with journal.phase("mystery"):
        pass
    op = journal.read()
    assert op is not None
    assert op.operation == "unknown"
    assert [p.name for p in op.phases] == ["mystery"]


def _reaped_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    assert child.wait(timeout=30) == 0
    return child.pid


def _op_with_pid(pid: int) -> journal.LifecycleOp:
    return journal.LifecycleOp(operation="stop", pid=pid, started_at=0.0, deadline=None)


def test_begin_takes_over_a_dead_writers_journal(home: Path) -> None:
    journal.begin("restart")
    with journal.phase("stop"):
        pass
    raw = json.loads(journal.status_path().read_text())
    raw["pid"] = _reaped_pid()
    journal.status_path().write_text(json.dumps(raw))

    assert journal.begin("restart") is True
    op = journal.read()
    assert op is not None
    assert op.operation == "restart" and op.complete is False
    assert op.pid == os.getpid()
    assert op.resumed_from is not None
    assert op.resumed_from.pid == raw["pid"]
    assert op.resumed_from.operation == "restart"
    assert [p.name for p in op.phases] == ["stop"]


def test_writer_alive_tracks_pid_state(home: Path) -> None:
    assert journal.writer_alive(_op_with_pid(os.getpid())) is True
    assert journal.writer_alive(_op_with_pid(_reaped_pid())) is False

    zombie = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if psutil.Process(zombie.pid).status() == psutil.STATUS_ZOMBIE:
                break
            time.sleep(0.01)
        assert psutil.Process(zombie.pid).status() == psutil.STATUS_ZOMBIE
        assert journal.writer_alive(_op_with_pid(zombie.pid)) is False
    finally:
        zombie.wait(timeout=30)


def test_journal_write_failure_never_breaks_the_operation(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    loguru_records: list[dict[str, object]],
) -> None:
    blocked = home / "blocked"
    blocked.write_text("not a directory")
    monkeypatch.setattr(journal, "journal_path", lambda: blocked / "lifecycle-op.json")

    journal.begin("stop")
    with journal.phase("drain"):
        pass
    journal.finish(0)

    assert journal.read() is None
    assert any("journal write failed" in str(record["message"]) for record in loguru_records)
