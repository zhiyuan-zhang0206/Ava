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

import ava
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
    write_request_file: bool = True,
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    """Write a request, run the real child, return (proc, request, result)."""
    exec_dir = tmp_path / "exec"
    request_path = make_request_path(exec_dir, agent_id=_AGENT_ID)
    result_path = make_result_path(exec_dir, agent_id=_AGENT_ID)
    if write_request_file:
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
    assert payload.attachments == []


def test_boot_config_failure_writes_crashed_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Settings failure while importing ava must reach the parent's result file."""
    for name in tuple(os.environ):
        if name.startswith("AVA_PITR_"):
            monkeypatch.delenv(name)
    home = tmp_path / "home"
    home.mkdir()
    backup_key = tmp_path / "backup.key"
    backup_key.write_bytes(b"k" * 32)
    backup_key.chmod(0o600)
    oss_credentials = tmp_path / "oss-credentials.json"
    oss_credentials.write_text(
        json.dumps({"access_key_id": "test-ak", "access_key_secret": "test-secret"}),
        encoding="utf-8",
    )
    oss_credentials.chmod(0o600)
    (home / ".env").write_text(
        "AVA_DB_URL=postgresql://u@127.0.0.1:1/x\n"
        "AVA_REDIS_URL=redis://127.0.0.1:1/0\n"
        "AVA_PITR_ENABLED=true\n"
        "AVA_PITR_BASE_BACKUP_ENABLED=true\n"
        "AVA_PITR_RESTORE_PROOF_ENABLED=true\n"
        "AVA_PITR_STORE_BACKEND=oss\n"
        "AVA_PITR_OSS_ENDPOINT=https://oss-cn-shanghai.aliyuncs.com\n"
        "AVA_PITR_OSS_BUCKET=some-bucket\n"
        f"AVA_PITR_OSS_CREDENTIALS_FILE={oss_credentials}\n"
        f"AVA_PITR_BACKUP_KEY_FILE={backup_key}\n"
        "AVA_PITR_BACKUP_KEY_ID=test\n"
        "AVA_PITR_REPLICATION_DB_URL=postgresql://repl@127.0.0.1:1/x\n",
        encoding="utf-8",
    )

    proc, _request, result = _spawn(tmp_path, "pass", write_request_file=False)

    assert proc.returncode == 0, proc.stderr
    payload = read_result(result)
    assert payload.kind == "crashed"
    assert payload.exc_type == "ValidationError"
    assert "viewer-only OSS credential" in (payload.exc_msg or "")
    assert "exec_child" in (payload.full_traceback or "")
    assert result.stat().st_mode & 0o777 == 0o600


def test_missing_request_writes_crashed_envelope_after_healthy_boot(tmp_path: Path) -> None:
    """A healthy child keeps reporting request-read failures through its envelope."""
    proc, _request, result = _spawn(tmp_path, "pass", write_request_file=False)

    assert proc.returncode == 0, proc.stderr
    payload = read_result(result)
    assert payload.kind == "crashed"
    assert payload.exc_type == "FileNotFoundError"
    assert "exec_child" in (payload.full_traceback or "")


def test_crash_envelope_uses_stdlib_fallback_when_protocol_writer_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed protocol writer cannot erase an already-caught child crash."""
    from agent import exec_child
    from agent.graph import _exec_protocol

    result = tmp_path / "result.json"

    def fail_write_result(_path: Path, _payload: object) -> None:
        raise OSError("synthetic protocol write failure")

    monkeypatch.setattr(_exec_protocol, "write_result", fail_write_result)
    exec_child._write_crashed_result(str(result), ValueError("fallback boom"))

    payload = read_result(result)
    assert payload.kind == "crashed"
    assert payload.exc_type == "ValueError"
    assert payload.exc_msg == "fallback boom"
    assert "ValueError: fallback boom" in (payload.full_traceback or "")
    assert result.stat().st_mode & 0o777 == 0o600


def test_child_builtin_help_routes_only_ava_targets(tmp_path: Path) -> None:
    code = """
import contextlib
import builtins
import io
import json
import os
import sys

import ava


def capture(call, stdin_text=None):
    output = io.StringIO()
    original_stdin = sys.stdin
    if stdin_text is not None:
        sys.stdin = io.StringIO(stdin_text)
    try:
        with contextlib.redirect_stdout(output):
            call()
    except BaseException as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "output": output.getvalue()}
    finally:
        sys.stdin = original_stdin
    return {"error": None, "output": output.getvalue()}


outputs = {
    "shim_is_global_builtin": help is builtins.help,
    "global_builtin_type": f"{type(builtins.help).__module__}.{type(builtins.help).__name__}",
    "ava_files": capture(lambda: help(ava.files)),
    "agent_status": capture(lambda: help(ava.agents.AgentStatus)),
    "str": capture(lambda: help("str")),
    "os_path": capture(lambda: help(os.path)),
    "no_args": capture(lambda: help(), "quit\\n"),
    "keyword": capture(lambda: help(request=ava.files)),
    "mixed": capture(lambda: help(ava.files, os.path)),
    "two_ava": capture(lambda: help(ava.files, ava.agents.AgentStatus)),
    "expected_ava_files": capture(lambda: ava.help(ava.files)),
    "expected_agent_status": capture(lambda: ava.help(ava.agents.AgentStatus)),
    "expected_str": capture(lambda: builtins.help("str")),
    "expected_os_path": capture(lambda: builtins.help(os.path)),
    "expected_no_args": capture(lambda: builtins.help(), "quit\\n"),
    "expected_keyword": capture(lambda: builtins.help(request=ava.files)),
    "expected_two_ava": capture(lambda: ava.help(ava.files, ava.agents.AgentStatus)),
}
print("__HELP_OUTPUTS__" + json.dumps(outputs, sort_keys=True))
"""
    proc, _request, result = _spawn(tmp_path, code)

    assert proc.returncode == 0, proc.stderr
    payload = read_result(result)
    assert payload.kind == "done", payload
    output_line = next(
        line for line in proc.stdout.splitlines() if line.startswith("__HELP_OUTPUTS__")
    )
    outputs = json.loads(output_line.removeprefix("__HELP_OUTPUTS__"))

    assert outputs["shim_is_global_builtin"] is False
    assert outputs["global_builtin_type"] == "_sitebuiltins._Helper"
    assert outputs["ava_files"] == outputs["expected_ava_files"]
    assert outputs["agent_status"] == outputs["expected_agent_status"]
    assert outputs["str"] == outputs["expected_str"]
    assert outputs["os_path"] == outputs["expected_os_path"]
    assert outputs["no_args"] == outputs["expected_no_args"]
    assert "Welcome to Python" in outputs["no_args"]["output"]
    assert outputs["keyword"] == outputs["expected_keyword"]
    assert outputs["mixed"] == {
        "error": None,
        "output": f"{outputs['expected_ava_files']['output']}\n"
        f"{outputs['expected_os_path']['output']}",
    }
    assert outputs["two_ava"] == outputs["expected_two_ava"]


def test_help_is_ava_target_accepts_only_agent_visible_sdk_objects() -> None:
    from ava._exports.help import is_ava_target
    from ava.skills import _NS, _Namespace

    def foreign_function() -> None:
        return None

    foreign_function.__module__ = "__main__"
    skill_namespace = _Namespace("example", _NS())

    assert is_ava_target(ava)
    assert is_ava_target(ava.files)
    assert is_ava_target(ava.files.read)
    assert is_ava_target(ava.agents.AgentStatus)
    assert is_ava_target(skill_namespace)
    assert not is_ava_target(str)
    assert not is_ava_target(int)
    assert not is_ava_target(os.path)
    assert not is_ava_target(object())
    assert not is_ava_target(foreign_function)


def test_child_attach_registration_reaches_result_envelope(tmp_path: Path) -> None:
    image = tmp_path / "render.png"
    image.write_bytes(b"png")
    proc, _request, result = _spawn(
        tmp_path,
        f"import ava\nava.self.attach({str(image)!r}, label='render result')",
        # attach is a media-capable-model feature (user ruling 2026-08-28):
        # the child's default test model is text-only and rejects the call.
        config_overlay={"llm_model": "deepseek-v4-flash-vision-exp"},
    )

    assert proc.returncode == 0, proc.stderr
    assert read_result(result).attachments == [
        {"path": str(image.resolve()), "label": "render result"}
    ]


def test_child_attach_rejected_for_text_only_model(tmp_path: Path) -> None:
    """A text-only-model child gets the model gate error, not a registration —
    `ava.self.attach` is unavailable to it (user ruling 2026-08-28)."""
    proc, _request, result = _spawn(
        tmp_path,
        "import ava\nava.self.attach('/tmp/render.png')",
        config_overlay={"llm_model": "deepseek-v4-pro"},
    )

    assert proc.returncode == 0, proc.stderr
    assert "ava.self.attach is unavailable" in proc.stdout
    assert "text-only" in proc.stdout
    payload = read_result(result)
    assert payload.kind == "crashed"
    assert "text-only" in (payload.exc_msg or "")


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
def test_child_installs_signal_handlers_before_reading_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A signal arriving during request decoding must become an in-band result,
    so SIGTERM's child handler is installed before the read begins."""
    from agent import exec_child
    from agent.graph import _exec_protocol
    from agent.graph._exec_protocol import RequestPayload, ResultPayload

    old_sigint = signal.getsignal(signal.SIGINT)
    old_sigterm = signal.getsignal(signal.SIGTERM)

    def fake_read_request(_path: Path) -> RequestPayload:
        handler = signal.getsignal(signal.SIGTERM)
        assert getattr(handler, "__name__", None) == "_raise_timeout_error"
        return RequestPayload(code="pass", agent_id=None, timeout_s=0.0, state=None)

    def fake_apply_scope(
        _birth: dict[str, object] | None,
        _overlay: dict[str, object] | None,
        *,
        scope: str,
    ) -> bool:
        return False

    def fake_build_state_slot(_state: dict[str, Any] | None) -> None:
        return None

    def fake_run_code(_code: str, _payload: ResultPayload) -> None:
        return None

    def fake_write_result(_path: Path, _payload: ResultPayload) -> None:
        return None

    monkeypatch.setattr(exec_child, "_line_buffered_output", lambda: None)
    monkeypatch.setattr(_exec_protocol, "read_request", fake_read_request)
    monkeypatch.setattr(exec_child, "_pop_overlay_env", lambda: (None, None))
    monkeypatch.setattr(exec_child, "_apply_overlay_scope", fake_apply_scope)
    monkeypatch.setattr(exec_child, "_build_state_slot", fake_build_state_slot)
    monkeypatch.setattr(exec_child, "_run_code", fake_run_code)
    monkeypatch.setattr(_exec_protocol, "write_result", fake_write_result)
    monkeypatch.setattr("ava._ensure_plugins_loaded", lambda: None)
    monkeypatch.setattr("ava.security.take_findings", list)
    monkeypatch.setattr("ava._attach.take_attachments", list)

    try:
        exec_child._run("request.json", "result.json")
    finally:
        signal.signal(signal.SIGINT, old_sigint)
        signal.signal(signal.SIGTERM, old_sigterm)


def test_child_boot_timing_emits_ready_duration(
    monkeypatch: pytest.MonkeyPatch, loguru_records: list[dict[str, Any]]
) -> None:
    """The child emits its own ready boundary, separating bootstrap cost from user code."""
    from agent import exec_child

    monkeypatch.setattr(exec_child, "_CHILD_BOOT_STARTED_AT", 100.0)
    monkeypatch.setattr(exec_child.time, "perf_counter", lambda: 100.25)

    exec_child._emit_child_boot_timing()

    [record] = [
        record for record in loguru_records if record["extra"].get("event") == "exec_child_boot"
    ]
    assert record["extra"]["duration_ms"] == pytest.approx(250.0)  # pyright: ignore[reportUnknownMemberType]


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
        birth_config={
            "llm_model": "deepseek-v4-flash-vision-exp",
            "llm_stream_ttft_timeout_seconds": 3.0,
        },
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
    from agent import exec_child
    from agent.graph import _exec_protocol
    from agent.graph._exec_protocol import RequestPayload, ResultPayload

    events: list[str] = []

    def fake_read_request(_path: Path) -> RequestPayload:
        return RequestPayload(code="pass", agent_id=None, timeout_s=0.0, state=None)

    def fake_init_logger(_agent_id: int | None) -> None:
        return None

    def fake_eval_isolation() -> None:
        events.append("eval_isolation")

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

    monkeypatch.setattr(_exec_protocol, "read_request", fake_read_request)
    monkeypatch.setattr(exec_child, "_init_logger", fake_init_logger)
    monkeypatch.setattr("agent._process_boot._apply_per_agent_sdk_disable", fake_sdk_disable)
    monkeypatch.setattr("agent._process_boot._apply_per_agent_eval_isolation", fake_eval_isolation)
    monkeypatch.setattr(exec_child, "_build_state_slot", fake_build_state_slot)
    monkeypatch.setattr(exec_child, "_run_code", fake_run_code)
    monkeypatch.setattr(_exec_protocol, "write_result", fake_write_result)
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
        "eval_isolation",
    ]


def test_child_help_hides_attach_for_text_only_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interactive help in a text-only child omits the attach contract — the
    SDK docs gate matches the system prompt (user ruling 2026-08-28)."""
    # help(ava.self) renders MACHINE_SPEC, which needs a machine identity —
    # the child's bare $AVA_HOME must carry its own machine_name file (env
    # identity is dropped when the home's .env does not declare it).
    (tmp_path / "home").mkdir(exist_ok=True)
    (tmp_path / "home" / "machine_name").write_text("test-host", encoding="utf-8")
    proc, _request, _result = _spawn(
        tmp_path,
        "import ava, io, contextlib\n"
        "buf = io.StringIO()\n"
        "with contextlib.redirect_stdout(buf):\n"
        "    ava.help(ava.self)\n"
        "print('HAS_ATTACH' if 'def attach(' in buf.getvalue() else 'NO_ATTACH')",
        config_overlay={"llm_model": "deepseek-v4-pro"},
    )

    assert proc.returncode == 0, proc.stderr
    assert "NO_ATTACH" in proc.stdout


def test_child_help_keeps_attach_for_media_capable_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A media-capable child's interactive help keeps the attach contract
    (user ruling 2026-08-28)."""
    # help(ava.self) renders MACHINE_SPEC, which needs a machine identity —
    # the child's bare $AVA_HOME must carry its own machine_name file (env
    # identity is dropped when the home's .env does not declare it).
    (tmp_path / "home").mkdir(exist_ok=True)
    (tmp_path / "home" / "machine_name").write_text("test-host", encoding="utf-8")
    proc, _request, _result = _spawn(
        tmp_path,
        "import ava, io, contextlib\n"
        "buf = io.StringIO()\n"
        "with contextlib.redirect_stdout(buf):\n"
        "    ava.help(ava.self)\n"
        "print('HAS_ATTACH' if 'def attach(' in buf.getvalue() else 'NO_ATTACH')",
        config_overlay={"llm_model": "deepseek-v4-flash-vision-exp"},
    )

    assert proc.returncode == 0, proc.stderr
    assert "HAS_ATTACH" in proc.stdout
