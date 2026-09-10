"""The lifecycle status journal: durable phase timeline + final diagnosis.

The whole point of the journal (issue #2123) is that it outlives the CLI
process: phases are written as they happen and the final result rides out even
when the operation fails or the process is cut off.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from shared import lifecycle_status as journal
from shared.config import settings


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(settings.general, "ava_home", str(tmp_path))
    return tmp_path


def test_begin_phase_finish_round_trip(home: Path) -> None:
    assert journal.begin("pause", deadline=time.monotonic() + 30) is True
    with journal.phase("drain"):
        pass
    journal.finish(0)
    op = journal.read()
    assert op is not None
    assert op.operation == "pause"
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
    # A restart wraps a stop leg; the stop's begin() must not replace the
    # restart's journal, and the stop's finish() must not close it either.
    assert journal.begin("restart") is True
    with journal.phase("preflight"):
        pass
    assert journal.begin("pause") is False  # active journal kept
    with journal.phase("stop"), journal.phase("drain"):
        pass
    op = journal.read()
    assert op is not None
    assert op.operation == "restart"
    assert op.complete is False
    assert [p.name for p in op.phases] == ["preflight", "stop", "drain"]


def test_completed_journal_can_be_replaced(home: Path) -> None:
    journal.begin("pause")
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
    # Parseable JSON with a wrong shape must read as "absent" too: `read()`
    # sits on the stop/pause/restart path, so a missing pid or a bad field
    # type must not raise KeyError/TypeError/ValueError mid-operation.
    path = journal.status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([{"operation": "pause"}]))  # not an object
    assert journal.read() is None
    path.write_text(json.dumps({"operation": "pause"}))  # missing pid
    assert journal.read() is None
    path.write_text(json.dumps({"operation": "pause", "pid": "x"}))  # bad type
    assert journal.read() is None
    path.write_text(json.dumps({"operation": "pause", "pid": 1, "started_at": []}))
    assert journal.read() is None
    path.write_text(json.dumps({"operation": "pause", "pid": 1, "deadline": "soon"}))
    assert journal.read() is None


def test_journal_survives_an_unfinished_process(home: Path) -> None:
    # The acceptance shape: the writer dies mid-phase; the last phase shows
    # started-but-not-finished and complete=False, so a later reader sees the
    # state and the missing final diagnosis instead of nothing.
    journal.begin("restart")
    with journal.phase("stop"):
        pass
    with pytest.raises(SystemExit), journal.phase("start"):
        # simulate the process being cut off inside the phase
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
    """A pid whose process has exited and been reaped (a dead journal writer)."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    assert child.wait(timeout=30) == 0
    return child.pid


def _op_with_pid(pid: int) -> journal.LifecycleOp:
    return journal.LifecycleOp(operation="pause", pid=pid, started_at=0.0, deadline=None)


def test_begin_takes_over_a_dead_writers_journal(home: Path) -> None:
    # The cut-off this journal exists for: a rerun must not keep writing under
    # the dead run's pid. begin() takes the journal over — refreshing the pid,
    # carrying the dead run's phase timeline forward, and recording the
    # superseded identity (task #2898).
    journal.begin("restart")
    with journal.phase("stop"):
        pass
    raw = json.loads(journal.status_path().read_text())
    raw["pid"] = _reaped_pid()  # simulate the writer dying mid-operation
    journal.status_path().write_text(json.dumps(raw))

    assert journal.begin("restart") is True
    op = journal.read()
    assert op is not None
    assert op.operation == "restart" and op.complete is False
    assert op.pid == os.getpid()
    assert op.resumed_from is not None
    assert op.resumed_from.pid == raw["pid"]
    assert op.resumed_from.operation == "restart"
    assert [p.name for p in op.phases] == ["stop"]  # the dead run's timeline survives


def test_writer_alive_tracks_pid_state(home: Path) -> None:
    assert journal._writer_alive(_op_with_pid(os.getpid())) is True
    assert journal._writer_alive(_op_with_pid(_reaped_pid())) is False

    # A zombie has finished executing and can never write again — but it still
    # holds the pid, so a liveness check that misses it would block takeover.
    if sys.platform == "win32":
        return
    zombie = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if psutil.Process(zombie.pid).status() == psutil.STATUS_ZOMBIE:
                break
            time.sleep(0.01)
        assert psutil.Process(zombie.pid).status() == psutil.STATUS_ZOMBIE
        assert journal._writer_alive(_op_with_pid(zombie.pid)) is False
    finally:
        zombie.wait(timeout=30)


def test_journal_write_failure_never_breaks_the_operation(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    loguru_records: list[dict[str, object]],
) -> None:
    # QA round-1 (#2898): the journal is best-effort by contract — a failing
    # write warns and the stop/restart proceeds. A parent that is a regular
    # file makes every mkdir inside _write fail.
    blocked = home / "blocked"
    blocked.write_text("not a directory")
    monkeypatch.setattr(journal, "_path", lambda: blocked / "lifecycle-op.json")

    journal.begin("pause")
    with journal.phase("drain"):
        pass
    journal.finish(0)

    assert journal.read() is None
    assert any("journal write failed" in str(record["message"]) for record in loguru_records)
