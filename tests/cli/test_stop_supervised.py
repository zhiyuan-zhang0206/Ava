"""Real OS/process evidence for no-escalation stop; exact temporary jobs only."""

from __future__ import annotations

import contextlib
import os
import plistlib
import subprocess
import sys
import time
import uuid
from pathlib import Path

import psutil
import pytest

from cli.commands._stop_supervised import stop_detached, stop_launchd, stop_systemd


def _ready(path: Path) -> int:
    deadline = time.monotonic() + 15
    while not path.exists():
        if time.monotonic() > deadline:
            pytest.fail("test service never started")
        time.sleep(0.05)
    return int(path.read_text())


def _child(tmp_path: Path, *, ignore: bool) -> Path:
    code = tmp_path / "service.py"
    code.write_text(
        "import os,pathlib,signal,time\n"
        + ("signal.signal(signal.SIGTERM, lambda *_: None)\n" if ignore else "")
        + f"pathlib.Path({str(tmp_path / 'ready')!r}).write_text(str(os.getpid()))\n"
        + "time.sleep(120)\n"
    )
    return code


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX daemon signal contract")
def test_detached_ignores_term_until_explicit_force(tmp_path: Path) -> None:
    child = subprocess.Popen([sys.executable, str(_child(tmp_path, ignore=True))])  # noqa: S603
    try:
        assert _ready(tmp_path / "ready") == child.pid
        with pytest.raises(TimeoutError, match="did not exit"):
            stop_detached(child.pid, force=False, timeout_s=0.1)
        assert child.poll() is None
        stop_detached(child.pid, force=True, timeout_s=3)
        child.wait(timeout=3)
        assert child.returncode != 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=3)


@pytest.mark.skipif(sys.platform != "darwin", reason="real launchd user service")
@pytest.mark.parametrize("ignore", [False, True])
def test_launchd_bootout_respects_cli_deadline(tmp_path: Path, ignore: bool) -> None:
    code = _child(tmp_path, ignore=ignore)
    label = "com.ava.test-stop-" + uuid.uuid4().hex
    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{label}"
    path = tmp_path / "service.plist"
    path.write_bytes(
        plistlib.dumps(
            {
                "Label": label,
                "ProgramArguments": [sys.executable, str(code)],
                "KeepAlive": True,
                "RunAtLoad": True,
                "ExitTimeOut": 10,
            }
        )
    )
    launched = subprocess.run(  # noqa: S603 — generated temporary exact launchd label
        ["launchctl", "bootstrap", domain, str(path)], capture_output=True, check=False, timeout=15
    )
    assert launched.returncode == 0, launched.stderr
    pid = _ready(tmp_path / "ready")
    try:
        if ignore:
            with pytest.raises(TimeoutError):
                stop_launchd(label, force=False, timeout_s=0.3)
            assert psutil.pid_exists(pid), "deadline must not secretly SIGKILL the service"
            stop_launchd(label, force=True, timeout_s=5)
        else:
            stop_launchd(label, force=False, timeout_s=5)
        assert not psutil.pid_exists(pid)
        assert path.exists(), "normal stop retains the next-start service definition"
        absent = subprocess.run(  # noqa: S603
            ["launchctl", "print", target], capture_output=True, check=False, timeout=5
        )
        assert absent.returncode == 113
    finally:
        with contextlib.suppress(psutil.NoSuchProcess):
            psutil.Process(pid).kill()
        subprocess.run(  # noqa: S603 — exact fixture job
            ["launchctl", "bootout", target], capture_output=True, check=False, timeout=5
        )


def test_launchd_inspection_failure_is_not_absence(monkeypatch: pytest.MonkeyPatch) -> None:
    from cli.commands import _stop_supervised as stops

    calls: list[str] = []

    def command(*args: str, timeout: float) -> subprocess.CompletedProcess[str]:
        calls.append(args[0])
        return subprocess.CompletedProcess(args, 5, "", "injected manager failure")

    monkeypatch.setattr(stops, "_launchctl", command)
    monkeypatch.setattr(stops.os, "getuid", lambda: 501, raising=False)
    with pytest.raises(RuntimeError, match="Cannot inspect"):
        stop_launchd("com.ava.test", force=False, timeout_s=1)
    assert calls == ["print"]


def test_systemd_stop_timeout_does_not_force_or_remove_enablement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cli.commands import _gate_systemd

    calls: list[tuple[str, ...]] = []

    def command(*args: str, timeout: float = 30) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(
            args, 0, "LoadState=loaded\nActiveState=deactivating\nMainPID=0\nSendSIGKILL=no\n", ""
        )

    monkeypatch.setattr(_gate_systemd, "_systemctl", command)
    with pytest.raises(TimeoutError):
        stop_systemd("com.ava.test.service", force=False, timeout_s=0.1)
    assert ("stop", "--no-block", "com.ava.test.service") in calls
    assert not any(call[0] in {"kill", "disable"} for call in calls)


def test_systemd_refuses_unsafe_supervisor(monkeypatch: pytest.MonkeyPatch) -> None:
    from cli.commands import _gate_systemd

    calls: list[tuple[str, ...]] = []

    def command(*args: str, timeout: float = 30) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(
            args, 0, "LoadState=loaded\nActiveState=active\nMainPID=0\nSendSIGKILL=yes\n", ""
        )

    monkeypatch.setattr(_gate_systemd, "_systemctl", command)
    with pytest.raises(RuntimeError, match="automatic force kill"):
        stop_systemd("com.ava.test.service", force=False, timeout_s=1)
    assert len(calls) == 1


def test_systemd_missing_unit_is_already_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    from cli.commands import _gate_systemd

    calls: list[tuple[str, ...]] = []

    def command(*args: str, timeout: float = 30) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        assert 0 < timeout <= 0.5
        return subprocess.CompletedProcess(args, 1, "LoadState=not-found\n", "unit does not exist")

    monkeypatch.setattr(_gate_systemd, "_systemctl", command)
    stop_systemd("com.ava.test.service", force=False, timeout_s=0.5)
    assert len(calls) == 1


def test_systemd_foreign_fragment_is_not_signalled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from cli.commands import _gate_systemd

    calls: list[tuple[str, ...]] = []

    def command(*args: str, timeout: float = 30) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(
            args, 0, "LoadState=loaded\nFragmentPath=/foreign/service\n", ""
        )

    monkeypatch.setattr(_gate_systemd, "_systemctl", command)
    with pytest.raises(RuntimeError, match="foreign user unit"):
        stop_systemd(
            "com.ava.test.service",
            force=False,
            timeout_s=1,
            expected_fragment=tmp_path / "owned.service",
        )
    assert len(calls) == 1
