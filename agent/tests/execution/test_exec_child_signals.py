"""Exec child signals retain timeout envelopes and initialize handlers before request decoding."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import ava
from agent.graph.exec.protocol import (
    make_request_path,
    make_result_path,
    read_result,
    write_request,
)
from agent.tests.execution.test_exec_child import _AGENT_ID, _child_env
from ava.sdk_surface.install import Installation
from tests.fixtures.pin_agent import exec_context


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
        context=exec_context(_AGENT_ID).describe(),
        timeout_s=60.0,
        state=None,
        incarnation=None,
    )
    env = _child_env(tmp_path, request_path, result_path)
    proc = subprocess.Popen(
        [sys.executable, "-I", "-X", "utf8", "-m", "agent.execution.child"],
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
    model_installation: Installation,
) -> None:
    """A signal arriving during request decoding must become an in-band result,
    so SIGTERM's child handler is installed before the read begins."""
    from agent.execution import child as exec_child
    from agent.graph.exec import protocol
    from agent.graph.exec.protocol import RequestPayload, ResultPayload

    old_sigint = signal.getsignal(signal.SIGINT)
    old_sigterm = signal.getsignal(signal.SIGTERM)

    def fake_read_request(_path: Path) -> RequestPayload:
        handler = signal.getsignal(signal.SIGTERM)
        assert getattr(handler, "__name__", None) == "_raise_timeout_error"
        return RequestPayload(
            code="pass", context=exec_context(None).describe(), timeout_s=0.0, state=None
        )

    def fake_apply_scope(
        _birth: dict[str, object] | None,
        _overlay: dict[str, object] | None,
        *,
        scope: str,
    ) -> bool:
        return False

    def fake_build_state_slot(_child: exec_child._ChildContext, _payload: RequestPayload) -> None:
        return None

    def fake_run_code(_code: str, _payload: ResultPayload) -> None:
        return None

    def fake_write_result(_path: Path, _payload: ResultPayload) -> None:
        return None

    def fake_ensure_plugins_loaded(*, surface: bool = True) -> None:
        # Stateless request (fake_read_request: state=None) -> the surface load.
        assert surface is True

    monkeypatch.setattr(exec_child, "_line_buffered_output", lambda: None)
    monkeypatch.setattr(protocol, "read_request", fake_read_request)
    monkeypatch.setattr(exec_child, "_pop_overlay_env", lambda: (None, None))
    monkeypatch.setattr(exec_child, "_apply_overlay_scope", fake_apply_scope)
    monkeypatch.setattr(exec_child, "_build_state_slot", fake_build_state_slot)
    monkeypatch.setattr(exec_child, "_run_code", fake_run_code)
    monkeypatch.setattr(protocol, "write_result", fake_write_result)
    monkeypatch.setattr("ava.ensure_plugins_loaded", fake_ensure_plugins_loaded)
    monkeypatch.setattr(ava, "__plugin_installation__", model_installation, raising=False)

    try:
        exec_child._run("request.json", "result.json", 0.0)
    finally:
        signal.signal(signal.SIGINT, old_sigint)
        signal.signal(signal.SIGTERM, old_sigterm)
