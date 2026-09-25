"""Preview evidence must reject execution failures and detect daemonized survivors."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.preview import runtime


@pytest.mark.parametrize("body", ["(no output)", "Traceback: error at line 3", "13", "3\nError"])
def test_timestamp_or_traceback_is_not_execution_success(body: str) -> None:
    items: list[runtime.TimelineItem] = [
        {"kind": "agent_code", "payload": "print(1 + 2)", "exec_ms": None},
        {"kind": "agent_chat", "payload": "done", "exec_ms": None},
        {
            "kind": "code_output",
            "exec_ms": 12,
            "payload": f"Code execution output [2026-09-25 13:00:00]:\n\n{body}",
        },
    ]
    assert not runtime.execution_completed(items, "done")
    items[-1]["payload"] = "Code execution output:\n\n3\n"
    assert runtime.execution_completed(items, "done")
    items[-1]["payload"] = "Code execution output [cancelled by user]:\n\n3\n"
    assert not runtime.execution_completed(items, "done")


def test_process_with_rewritten_argv_still_belongs_to_home(tmp_path: Path) -> None:
    data = tmp_path / "home/redis"
    data.mkdir(parents=True)
    # A child with no home in argv models daemonized Redis's rewritten title.
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], cwd=data)
    try:
        assert child.pid in {p.pid for p in runtime.owned_processes(tmp_path)}
        assert child.pid not in {p.pid for p in runtime.owned_processes(tmp_path / "neighbor")}
        assert child.poll() is None
    finally:
        child.kill()
        child.wait()


def test_recorded_real_execution_is_checked_exactly() -> None:
    # The envelope shape includes a timestamp, while exec_ms proves this is an
    # execution result item rather than the scripted model's final claim.
    output: runtime.TimelineItem = {
        "kind": "code_output",
        "payload": "Code execution output [13:03]:\n\n3\n",
        "exec_ms": 573,
    }
    items: list[runtime.TimelineItem] = [
        {"kind": "agent_code", "payload": "print(1 + 2)\n", "exec_ms": None},
        output,
        {"kind": "agent_chat", "payload": "done", "exec_ms": None},
    ]
    assert runtime.execution_completed(json.loads(json.dumps(items)), "done")
    output["exec_ms"] = None
    assert not runtime.execution_completed(items, "done")


def test_cleanup_observer_cannot_release_a_remaining_registry_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    registry = tmp_path / "clusters.json"
    original = json.dumps({str(home): {"gateway_home": str(home)}})
    registry.write_text(original)
    monkeypatch.setattr(runtime, "owned_processes", lambda _run: [])

    with pytest.raises(RuntimeError, match="registry slot remains"):
        runtime.verify_stopped(tmp_path, home)

    assert registry.read_text() == original
    assert not (tmp_path / "cleanup.json").exists()
