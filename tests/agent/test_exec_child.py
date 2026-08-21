"""Direct-spawn tests for the exec child entry (`agent/exec_child.py`) —
child-side behaviors the parent machinery cannot observe on its own: the
SIGTERM -> TimeoutError -> timed_out envelope path, the watchdog hard-exit,
the state-slot injection (plugin namespace reads the snapshot), the
plugin-delta round-trip, and the lifecycle envelope.

Each test spawns a real child with a tmp AVA_HOME, so `import ava` + plugin
load costs ~1s per spawn — keep the count low and each spawn meaningful.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import HumanMessage

from agent.graph._exec_protocol import (
    make_request_path,
    make_result_path,
    read_result,
    write_request,
)

# Fixed test identity — the child never dials a real DB/Redis here.
_AGENT_ID = 424242


def _child_env(tmp_path: Path, request_path: Path, result_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "AVA_HOME": str(tmp_path / "home"),
            "AVA_AGENT_ID": str(_AGENT_ID),
            "AVA_PROCESS_PROFILE": "agent",
            "AVA_EXEC_REQUEST_FILE": str(request_path),
            "AVA_EXEC_RESULT_FILE": str(result_path),
            # Fast watchdog for the watchdog test (default margin is 5s).
            "AVA_EXEC_WATCHDOG_MARGIN_S": "0.5",
        }
    )
    return env


def _spawn(
    tmp_path: Path, code: str, *, timeout_s: float = 60.0, state: dict[str, Any] | None = None
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    """Write a request, run the real child, return (proc, request, result)."""
    exec_dir = tmp_path / "exec"
    request_path = make_request_path(exec_dir, agent_id=_AGENT_ID)
    result_path = make_result_path(exec_dir, agent_id=_AGENT_ID)
    write_request(request_path, code=code, agent_id=_AGENT_ID, timeout_s=timeout_s, state=state)
    proc = subprocess.run(
        [sys.executable, "-m", "agent.exec_child"],
        capture_output=True,
        text=True,
        env=_child_env(tmp_path, request_path, result_path),
        timeout=120,
        check=False,
    )
    return proc, request_path, result_path


def test_child_simple_code_done_envelope(tmp_path: Path) -> None:
    proc, request, result = _spawn(tmp_path, "print('hello exec child')")
    assert proc.returncode == 0, proc.stderr
    assert "hello exec child" in proc.stdout
    assert request.exists()  # child leaves it; the parent machinery cleans up
    payload = read_result(result)
    assert payload.kind == "done"
    assert payload.state_update is None
    assert payload.findings == []


def test_child_crash_writes_envelope_with_traceback(tmp_path: Path) -> None:
    proc, _request, result = _spawn(tmp_path, "raise ValueError('boom')")
    assert proc.returncode == 0
    # agent-facing traceback goes to the output pipe (framework logs do not)
    assert "ValueError: boom" in proc.stdout
    assert "Traceback" in proc.stdout
    payload = read_result(result)
    assert payload.kind == "crashed"
    assert payload.exc_type == "ValueError"
    assert "boom" in (payload.exc_msg or "")
    assert "ValueError" in (payload.full_traceback or "")


def test_child_state_slot_and_plugin_delta_round_trip(tmp_path: Path) -> None:
    """The snapshot is injected into ava.state; the plugin namespace reads it,
    a plugin write lands in the delta, and both cross back to the parent."""
    state = {
        "messages": [HumanMessage(content="hi from snapshot")],
        "halted": False,
    }
    proj = tmp_path / "proj"
    proj.mkdir()
    code = f"""
import ava
print("messages in snapshot:", len(ava.state.messages))
print("first message:", ava.state.messages[0].content)
ava.cwd.set({str(proj)!r})
print("cwd after set:", ava.cwd.get())
"""
    proc, _request, result = _spawn(tmp_path, code, state=state)
    assert proc.returncode == 0, proc.stderr
    assert "first message: hi from snapshot" in proc.stdout
    assert "cwd after set:" in proc.stdout
    payload = read_result(result)
    assert payload.kind == "done"
    assert payload.state_update is not None
    assert payload.state_update.get("ava_code__cwd") == str(proj)
    assert payload.state_update.get("ava_code__cwd_note") is not None


def test_child_lifecycle_envelope(tmp_path: Path) -> None:
    """A `_LifecycleExit` raised by agent code becomes a lifecycle outcome with
    the class name — the parent reconstructs the exception from it."""
    proc, _request, result = _spawn(
        tmp_path, "from shared.lifecycle import AgentRestart\nraise AgentRestart()"
    )
    assert proc.returncode == 0
    payload = read_result(result)
    assert payload.kind == "lifecycle"
    assert payload.lifecycle_type == "AgentRestart"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_child_sigterm_writes_timed_out_envelope(tmp_path: Path) -> None:
    """SIGTERM -> TimeoutError inside the child -> kind=timed_out envelope with
    partial output preserved."""
    exec_dir = tmp_path / "exec"
    request_path = make_request_path(exec_dir, agent_id=_AGENT_ID)
    result_path = make_result_path(exec_dir, agent_id=_AGENT_ID)
    write_request(
        request_path,
        code="import time\nprint('before sleep', flush=True)\ntime.sleep(60)",
        agent_id=_AGENT_ID,
        timeout_s=60.0,
        state=None,
    )
    env = _child_env(tmp_path, request_path, result_path)
    proc = subprocess.Popen(
        [sys.executable, "-m", "agent.exec_child"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    # Wait for the child to reach the sleep (line-buffered pipe), then signal.
    assert proc.stdout is not None
    deadline = time.monotonic() + 30
    out = ""
    while "before sleep" not in out:
        if time.monotonic() > deadline:
            proc.kill()
            pytest.fail(f"child never reached the sleep; output so far: {out!r}")
        out += proc.stdout.readline()
    os.kill(proc.pid, signal.SIGTERM)
    out += proc.stdout.read()
    assert proc.wait(timeout=60) == 0
    assert "before sleep" in out
    payload = read_result(result_path)
    assert payload.kind == "timed_out"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_child_watchdog_hard_exits_124(tmp_path: Path) -> None:
    """With no parent to signal it, the child's own watchdog os._exit(124)s
    past (timeout + grace + margin) — an orphaned exec child cannot run long."""
    proc, _request, result = _spawn(tmp_path, "import time\ntime.sleep(60)", timeout_s=0.2)
    assert proc.returncode == 124
    assert not result.exists()  # hard exit skips the envelope — parent classifies
