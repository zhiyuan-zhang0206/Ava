"""Direct-spawn tests for the exec child entry (`agent/exec_child.py`) —
child-side behaviors the parent machinery cannot observe on its own: the
SIGTERM -> TimeoutError -> timed_out envelope path, the watchdog hard-exit,
the state-slot injection (plugin namespace reads the snapshot), the
plugin-delta round-trip, and the lifecycle envelope.

Each test spawns a real child with a tmp AVA_HOME, so `import ava` + plugin
load costs ~1s per spawn — keep the count low and each spawn meaningful.
"""

from __future__ import annotations

import json
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


def _child_env(
    tmp_path: Path,
    request_path: Path,
    result_path: Path,
    *,
    config_overlay: dict[str, object] | None = None,
    birth_config: dict[str, object] | None = None,
) -> dict[str, str]:
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
    if config_overlay is not None:
        env["AVA_AGENT_CONFIG_OVERLAY"] = json.dumps(config_overlay, sort_keys=True)
    if birth_config is not None:
        env["AVA_AGENT_BIRTH_CONFIG"] = json.dumps(birth_config, sort_keys=True)
    return env


def _spawn(
    tmp_path: Path,
    code: str,
    *,
    timeout_s: float = 60.0,
    state: dict[str, Any] | None = None,
    config_overlay: dict[str, object] | None = None,
    birth_config: dict[str, object] | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    """Write a request, run the real child, return (proc, request, result)."""
    exec_dir = tmp_path / "exec"
    request_path = make_request_path(exec_dir, agent_id=_AGENT_ID)
    result_path = make_result_path(exec_dir, agent_id=_AGENT_ID)
    write_request(request_path, code=code, agent_id=_AGENT_ID, timeout_s=timeout_s, state=state)
    proc = subprocess.run(
        [sys.executable, "-I", "-X", "utf8", "-m", "agent.exec_child"],
        capture_output=True,
        text=True,
        env=_child_env(
            tmp_path,
            request_path,
            result_path,
            config_overlay=config_overlay,
            birth_config=birth_config,
        ),
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
from pathlib import Path
import ava
process_cwd = Path.cwd()
print("messages in snapshot:", len(ava.state.messages))
print("first message:", ava.state.messages[0].content)
ava.cwd.set({str(proj)!r})
print("cwd after set:", ava.cwd.get())
print("process cwd stable:", Path.cwd() == process_cwd)
"""
    proc, _request, result = _spawn(tmp_path, code, state=state)
    assert proc.returncode == 0, proc.stderr
    assert "first message: hi from snapshot" in proc.stdout
    assert "cwd after set:" in proc.stdout
    assert "process cwd stable: True" in proc.stdout
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
        [sys.executable, "-I", "-X", "utf8", "-m", "agent.exec_child"],
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


def test_child_applies_overlay_framework_and_pops_env(tmp_path: Path) -> None:
    """The re-emitted per-agent config maps reach the child's effective
    settings — the child's SDK calls resolve the same configuration the agent
    process booted with. The maps are also popped from the child's env so its
    own children do not inherit them."""
    proc, _request, result = _spawn(
        tmp_path,
        (
            "from shared.config import settings\n"
            "print(settings.lm.llm_model)\n"
            "print(settings.lm.llm_stream_ttft_timeout_seconds)\n"
            "import os\nprint(os.environ.get('AVA_AGENT_CONFIG_OVERLAY', 'GONE'))\n"
        ),
        config_overlay={"llm_model": "deepseek-v4-pro"},
        # per_agent=True field (overlay validation rejects non-per_agent keys).
        birth_config={"llm_stream_ttft_timeout_seconds": 3.0},
    )
    assert proc.returncode == 0, proc.stderr
    payload = read_result(result)
    assert payload.kind == "done"
    assert "deepseek-v4-pro" in proc.stdout
    assert "3.0" in proc.stdout
    # The maps were popped: the agent's code no longer sees them in env.
    assert "GONE" in proc.stdout


def test_child_overlay_phases_framework_then_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_run` applies the maps in the agent process's own boot order: framework
    scope (with the sdk_disable re-apply) BEFORE plugins load, plugin scope
    after. A single framework-only pass (the PR1 shape) silently dropped
    plugin-scope overlay fields — this locks the sequencing."""
    from types import SimpleNamespace

    from agent import exec_child
    from agent.graph._exec_protocol import RequestPayload, ResultPayload

    events: list[str] = []

    def fake_read_request(_path: Path) -> RequestPayload:
        return RequestPayload(code="pass", agent_id=None, timeout_s=0.0, state=None)

    def fake_establish(*_args: object, **_kwargs: object) -> None:
        return None

    def fake_init_logger(_agent_id: int | None) -> None:
        return None

    def fake_sdk_disable() -> None:
        events.append("sdk_disable")

    def fake_build_state_slot(_state: dict[str, Any] | None) -> None:
        return None

    def fake_run_code(_code: str, _payload: ResultPayload) -> None:
        return None

    def fake_write_result(_path: Path, _payload: ResultPayload) -> None:
        return None

    def fake_take_findings() -> list[object]:
        return []

    def fake_plugins_loaded() -> None:
        events.append("plugins")

    monkeypatch.setattr(exec_child, "read_request", fake_read_request)
    monkeypatch.setattr(exec_child, "_boot", SimpleNamespace(establish=fake_establish))
    monkeypatch.setattr(exec_child, "_init_logger", fake_init_logger)
    monkeypatch.setattr("agent._process_boot._apply_per_agent_sdk_disable", fake_sdk_disable)
    monkeypatch.setattr(exec_child, "_build_state_slot", fake_build_state_slot)
    monkeypatch.setattr(exec_child, "_run_code", fake_run_code)
    monkeypatch.setattr(exec_child, "write_result", fake_write_result)
    monkeypatch.setattr("ava.security.take_findings", fake_take_findings)
    monkeypatch.setattr("ava._ensure_plugins_loaded", fake_plugins_loaded)

    def fake_apply_scope(
        birth: dict[str, object] | None,
        overlay: dict[str, object] | None,
        *,
        scope: str,
    ) -> bool:
        events.append(f"apply:{scope}")
        return bool(birth or overlay)

    monkeypatch.setattr(exec_child, "_apply_overlay_scope", fake_apply_scope)
    monkeypatch.setenv("AVA_AGENT_CONFIG_OVERLAY", json.dumps({"llm_model": "x"}))
    monkeypatch.setenv(
        "AVA_AGENT_BIRTH_CONFIG", json.dumps({"llm_stream_ttft_timeout_seconds": 1.0})
    )
    monkeypatch.setenv("AVA_EXEC_REQUEST_FILE", str(tmp_path / "req.json"))
    monkeypatch.setenv("AVA_EXEC_RESULT_FILE", str(tmp_path / "res.json"))

    exec_child.main()

    assert events == [
        "apply:framework",
        "sdk_disable",
        "plugins",
        "apply:plugin",
    ]
