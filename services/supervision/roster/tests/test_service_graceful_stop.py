from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import psutil
import pytest

from base.sessions import posixproc
from base.sessions.backend import PosixProcSessionBackend
from base.sessions.env_forwarding import exec_into

_DAEMON = """
import signal, sys, time

def _stop(signum, frame):
    raise KeyboardInterrupt

signal.signal(signal.SIGTERM, _stop)
open({started!r}, "w").write("up")
try:
    while True:
        time.sleep(0.05)
except KeyboardInterrupt:
    pass
finally:
    open({marker!r}, "w").write("clean")
"""

_DEAF = """
import signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
open({started!r}, "w").write("up")
while True:
    time.sleep(0.05)
"""


def _wait_for(path: Path, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.02)
    return False


def _launch(name: str, script: Path, tmp_path: Path) -> None:
    ok = PosixProcSessionBackend().new_session(
        name, f"{sys.executable} {script}", tmp_path, env=dict(os.environ)
    )
    assert ok is True


def _recorded(name: str) -> psutil.Process:
    rec = posixproc.read_record(name)
    assert rec is not None, f"no session record for {name}"
    return psutil.Process(rec.pid)


def test_login_shell_session_records_the_daemon_not_a_wrapper_shell(tmp_path: Path, unit_home):
    del unit_home
    started, marker = tmp_path / "started", tmp_path / "clean"
    script = tmp_path / "daemon.py"
    script.write_text(_DAEMON.format(started=str(started), marker=str(marker)))
    _launch("meiva-test-shape", script, tmp_path)
    try:
        assert _wait_for(started), "daemon never started"
        proc = _recorded("meiva-test-shape")
        cmdline = " ".join(proc.cmdline())
        assert str(script) in cmdline, f"recorded pid is not the daemon: {cmdline!r}"
        assert "bash" not in proc.name(), (
            f"a wrapper shell survived as the recorded pid: {cmdline!r}"
        )
        descendants = [(p.pid, p.name()) for p in proc.children(recursive=True)]
        shells = [d for d in descendants if d[1] in {"sh", "bash", "dash", "zsh"}]
        assert not shells, f"a shell survives under the daemon: {shells} (all: {descendants})"
        assert posixproc.has_session("meiva-test-shape")
    finally:
        posixproc.kill_session("meiva-test-shape", graceful=False)


def test_graceful_signal_reaches_the_daemon_and_runs_its_cleanup(tmp_path: Path, unit_home):
    del unit_home
    started, marker = tmp_path / "started", tmp_path / "clean"
    script = tmp_path / "daemon.py"
    script.write_text(_DAEMON.format(started=str(started), marker=str(marker)))
    _launch("meiva-test-graceful", script, tmp_path)
    try:
        assert _wait_for(started), "daemon never started"
        t0 = time.monotonic()
        assert posixproc.graceful_signal("meiva-test-graceful") is True
        assert _wait_for(marker, timeout=10.0), (
            "the daemon never ran its finally — SIGTERM did not reach it"
        )
        elapsed = time.monotonic() - t0
        assert elapsed < 5.0, f"daemon took {elapsed:.1f}s to react to SIGTERM"
    finally:
        posixproc.kill_session("meiva-test-graceful", graceful=False)


def test_kill_session_reports_forced_when_the_daemon_ignores_sigterm(tmp_path: Path, unit_home):
    del unit_home
    started = tmp_path / "started"
    script = tmp_path / "deaf.py"
    script.write_text(_DEAF.format(started=str(started)))
    _launch("meiva-test-deaf", script, tmp_path)
    try:
        assert _wait_for(started), "daemon never started"
        ok, mode = posixproc.kill_session("meiva-test-deaf", graceful=True, timeout=1.0)
        assert ok is True, "the SIGKILL fallback must still confirm the process is gone"
        assert mode == "forced"
    finally:
        posixproc.kill_session("meiva-test-deaf", graceful=False)


def test_install_graceful_shutdown_raises_keyboardinterrupt():
    import signal

    from base.daemon.shutdown import install_graceful_shutdown

    previous = signal.getsignal(signal.SIGTERM)
    try:
        install_graceful_shutdown("unit-test")
        cleanup_ran = False
        with pytest.raises(KeyboardInterrupt):
            try:
                os.kill(os.getpid(), signal.SIGTERM)
                for _ in range(1000):
                    pass
            finally:
                cleanup_ran = True
        assert cleanup_ran, "the interrupt must unwind through the daemon's finally"
    finally:
        signal.signal(signal.SIGTERM, previous)


def test_exec_into_prefixes_a_simple_command():
    assert exec_into(".venv/bin/python -m gateway") == "exec .venv/bin/python -m gateway"


def test_exec_into_passes_through_a_command_that_execs_itself():
    cmd = "cd ui/web && npm run build && exec npm run start -- -p 3001"
    assert exec_into(cmd) == cmd


def test_frontend_service_command_is_exec_safe():
    from base.cluster import frontend_service_cmd

    cmd = frontend_service_cmd(3001, gateway_port=8000, browser_origin="")
    assert "exec npm run start -- -p 3001" in cmd
    assert exec_into(cmd) == cmd


@pytest.mark.parametrize(
    "cmd",
    [
        'echo "$PATH" > /tmp/path.txt',
        'curl "http://host/x?a=1&b=2"',
    ],
)
def test_exec_into_allows_single_commands_that_merely_look_compound(cmd: str):
    assert exec_into(cmd) == f"exec {cmd}"


@pytest.mark.parametrize(
    "cmd",
    [
        'python -c "exec (x)"',
        'foo --mode "exec now"',
    ],
)
def test_exec_into_does_not_mistake_a_stray_exec_word_for_a_handover(cmd: str):
    assert exec_into(cmd) == f"exec {cmd}"


def test_exec_into_rejects_an_exec_that_is_not_the_final_stage():
    with pytest.raises(ValueError, match="compound but never execs"):
        exec_into("exec npm run build && npm run start")


def test_exec_into_rejects_a_compound_command_that_never_execs():
    with pytest.raises(ValueError, match="compound but never execs"):
        exec_into("npm run build && npm run start")


def test_orchestration_session_keeps_its_wrapper_shell(monkeypatch: pytest.MonkeyPatch, unit_home):
    del unit_home
    seen: list[str] = []

    def _fake_new(_name: str, cmd: str, _cwd: Path, **_kw: object) -> bool:
        seen.append(cmd)
        return True

    monkeypatch.setattr(posixproc, "new_session", _fake_new)
    backend = PosixProcSessionBackend()
    pipeline = "update-command | tee -a log"
    assert backend.new_session("meiva-updater", pipeline, Path("/repo"), env={}, exec_cmd=False)
    assert seen[0].endswith(f"&& {pipeline}'")
    assert f"exec {pipeline}" not in seen[0]
    assert seen[0].startswith("exec bash -lc ")


def test_every_service_spec_command_is_exec_safe():
    from services.supervision.roster import build_services

    for spec in build_services():
        exec_into(spec.cmd)
