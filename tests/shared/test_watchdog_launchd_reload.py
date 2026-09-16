"""Model launchd's asynchronous removal without touching the OS scheduler."""

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from shared import os_watchdog_probe as probe


@pytest.fixture
def plist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "watchdog.plist"
    monkeypatch.setattr(probe, "_home_slug", lambda: "test")

    def plist_path(*_args: object) -> Path:
        return path

    def plist_content(*_args: object) -> str:
        return "new spec"

    monkeypatch.setattr(probe, "_plist_path", plist_path)
    monkeypatch.setattr(probe, "_plist_content", plist_content)
    monkeypatch.setattr("shared.platform.launchd_job_label", lambda: None)
    return path


def test_unchanged_loaded_job_is_not_unloaded(plist: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plist.write_text("new spec")
    before = plist.stat().st_mtime_ns
    calls: list[str] = []

    def run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd[1])
        assert cmd[1] == "print", "an unchanged loaded watchdog must survive converge"
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(probe.subprocess, "run", run)
    assert probe._register_macos("agent-runner", 60) == 0
    assert calls == ["print"]
    assert plist.stat().st_mtime_ns == before


def test_changed_job_waits_for_actual_removal(plist: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plist.write_text("old spec")
    prints = iter([0, 0, 0, 113])
    unloaded = False
    waits: list[float] = []

    def run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal unloaded
        if cmd[1] == "print":
            rc = next(prints)
            unloaded = rc == 113
            return subprocess.CompletedProcess(cmd, rc, "", "")
        if cmd[1] == "bootstrap":
            assert unloaded, "bootstrap before asynchronous removal reproduces EIO"
            assert plist.read_text() == "new spec"
        else:
            assert cmd[1] == "bootout"
            assert plist.read_text() == "old spec"
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(probe.subprocess, "run", run)
    monkeypatch.setattr(
        probe, "time", SimpleNamespace(monotonic=lambda: 0, sleep=waits.append), raising=False
    )
    assert probe._register_macos("agent-runner", 90) == 0
    assert waits == [0.1, 0.1]


def test_unload_timeout_preserves_old_spec(plist: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plist.write_text("old spec")
    clock = iter([0.0, 11.0])

    def run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        assert cmd[1] != "bootstrap"
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(probe.subprocess, "run", run)
    monkeypatch.setattr(
        probe, "time", SimpleNamespace(monotonic=lambda: next(clock)), raising=False
    )
    with pytest.raises(RuntimeError, match="did not unload"):
        probe._register_macos("agent-runner", 90)
    assert plist.read_text() == "old spec"


def test_probe_error_is_not_absence(plist: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plist.write_text("old spec")

    def run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        assert cmd[1] == "print"
        return subprocess.CompletedProcess(cmd, 5, "", "I/O error")

    monkeypatch.setattr(probe.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="cannot inspect"):
        probe._register_macos("agent-runner", 90)
    assert plist.read_text() == "old spec"


def test_probe_does_not_unload_its_own_ancestor(
    plist: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plist.write_text("old spec")
    monkeypatch.setattr(
        "shared.platform.launchd_job_label", lambda: probe.probe_label("agent-runner", "test")
    )

    def run(*_args: object, **_kwargs: object) -> None:
        pytest.fail("must not unload the job that owns this process tree")

    monkeypatch.setattr(probe.subprocess, "run", run)
    assert probe._register_macos("agent-runner", 90) == 0
    assert plist.read_text() == "old spec"
