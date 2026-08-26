"""ava.shell.run_background + the shared background-notice plumbing
(ava/shell/_background.py).

The e2e tests run through the real PTY supervisor daemon + real bash
(`_pty_sessions_env` fixture); POSIX-only, skip on Windows."""

import os
import time
from pathlib import Path
from typing import Any

import pytest

import ava
from ava.shell import _background
from shared.platform import IS_WINDOWS

pytestmark = [
    pytest.mark.skipif(IS_WINDOWS, reason="PTY supervisor is POSIX-only"),
    pytest.mark.usefixtures("_pty_sessions_env", "_isolated_agent"),
]


# -- notified_line (pure string construction) ----------------------------------


def test_notified_line_structure(tmp_path: Path) -> None:
    log = tmp_path / "x.log"
    line = _background.notified_line(
        "make build",
        agent_id=5,
        label="Background command 'build'",
        source="shell:3",
        output_path=log,
        keep=False,
    )
    # Subshell so the redirect covers a compound cmd; exit code pinned before
    # anything else runs; CLI notice with source + tail; close after delivery.
    assert line.startswith(f"( make build ) > {log} 2>&1; _ec=$?; ")
    assert "agents send 5" in line
    assert "Background command 'build' exited with code ${_ec}" in line
    assert "--source shell:3" in line
    assert f"--tail-file {log}" in line
    assert line.endswith("; exit $_ec")


def test_notified_line_keep_leaves_session_open(tmp_path: Path) -> None:
    line = _background.notified_line(
        "sleep 1",
        agent_id=5,
        label="Background command 'nap'",
        source="shell:3",
        output_path=tmp_path / "x.log",
        keep=True,
    )
    assert not line.endswith("&& exit")
    assert not line.endswith("; exit $_ec")
    assert "agents send 5" in line


def test_notified_line_rejects_multiline_cmd(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="single line"):
        _background.notified_line(
            "a\nb",
            agent_id=5,
            label="x",
            source="shell:3",
            output_path=tmp_path / "x.log",
            keep=False,
        )


def test_notified_line_rejects_shell_active_label(tmp_path: Path) -> None:
    # label rides inside the double-quoted notice where only ${_ec} may expand;
    # shell-active characters must be rejected here, not just by callers.
    with pytest.raises(ValueError, match="shell-active"):
        _background.notified_line(
            "echo hi",
            agent_id=5,
            label='x"; rm -rf ~; echo "',
            source="shell:3",
            output_path=tmp_path / "x.log",
            keep=False,
        )


def test_allocate_output_path_never_evicts_live_session_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A long-lived watcher's log keeps its spawn-time mtime forever (a time
    # watcher writes nothing to stdout), so mtime-LRU alone would evict it
    # while the session still runs — live sessions outrank the ring cap.
    from ava.shell import sessions as _sessions

    d = _background.output_dir()
    d.mkdir(parents=True, exist_ok=True)
    for old_log in d.glob("*.log"):
        old_log.unlink()
    base = time.time() - 3600
    live_log = d / "7_cron.log"  # the oldest file, owned by live session 7
    live_log.write_text("standing watcher")
    os.utime(live_log, (base - 100, base - 100))
    for i in range(_background._OUTPUT_KEEP + 5):
        p = d / f"{100 + i}_seed.log"
        p.write_text("old")
        os.utime(p, (base + i, base + i))
    monkeypatch.setattr(_sessions, "list", lambda: {7: "cron"})  # pyright: ignore[reportUnknownArgumentType]
    _background.allocate_output_path(999, "fresh")
    assert live_log.exists()


def test_allocate_output_path_prunes_ring() -> None:
    # Logs live at a predictable spot: .shell_logs/<sid>_<name>.log in the
    # agent's workspace (the default base of the file/shell tools).
    d = _background.output_dir()
    d.mkdir(parents=True, exist_ok=True)
    for old_log in d.glob("*.log"):
        old_log.unlink()
    # Seed more logs than the ring keeps, with strictly increasing mtimes.
    base = time.time() - 3600
    for i in range(_background._OUTPUT_KEEP + 5):
        p = d / f"{i}_seed.log"
        p.write_text("old")
        os.utime(p, (base + i, base + i))
    path = _background.allocate_output_path(999, "fresh")
    assert path == d / "999_fresh.log"
    assert path.exists()  # touched up front: tailable before the command writes
    remaining = list(d.glob("*.log"))
    assert len(remaining) <= _background._OUTPUT_KEEP
    assert path in remaining  # the newest survives the prune


# -- run_background ------------------------------------------------------------


def test_run_background_rejects_empty_cmd() -> None:
    with pytest.raises(ValueError, match="cmd cannot be empty"):
        ava.shell.run_background("   ", name="test-empty")


def test_run_background_requires_name() -> None:
    with pytest.raises(TypeError):
        ava.shell.run_background("echo hi")  # type: ignore[call-arg]


def test_run_background_line_and_handle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from ava.shell import sessions as _sessions

    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        _sessions,
        "_create_session",
        lambda _name, cwd=None, ttl=None: (7, "full"),  # noqa: ARG005 — cwd/ttl are part of the patched signature  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(_sessions, "send", lambda _id, cmd: captured.update(cmd=cmd))  # pyright: ignore[reportUnknownArgumentType]

    handle = ava.shell.run_background("echo hi", name="test-bg", cwd=str(tmp_path))
    assert handle.session_id == 7
    assert Path(handle.output_path).exists()  # tailable immediately
    cmd = captured["cmd"]
    assert "( echo hi )" in cmd
    assert "--source shell:7" in cmd
    assert cmd.endswith("; exit $_ec")


@pytest.mark.flaky  # real pty session + time.sleep polling (15s deadline)
def test_run_background_e2e_notice_log_and_close(
    _agent_row: int, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End-to-end through a real pty session: output lands in the log file, the
    completion notice fires with the shell:N source, and the session closes
    itself. The CLI is faked with a script that records its argv."""
    argv_file = tmp_path / "argv.txt"
    fake_cli = tmp_path / "fake-ava"
    fake_cli.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {argv_file}\n")
    fake_cli.chmod(0o755)
    monkeypatch.setattr(_background, "cli_path", lambda: fake_cli)  # pyright: ignore[reportUnknownArgumentType]

    handle = ava.shell.run_background("echo hello-bg; exit 3", name="test-bg", cwd=str(tmp_path))

    deadline = time.time() + 15
    while time.time() < deadline and not argv_file.exists():
        time.sleep(0.3)
    assert argv_file.exists(), "completion notice never fired"

    assert "hello-bg" in Path(handle.output_path).read_text()
    argv = argv_file.read_text().splitlines()
    assert argv[0] == "agents"
    assert argv[1] == "send"
    assert argv[2] == str(ava.self.AGENT_ID)
    assert "exited with code 3" in argv[3]  # subshell exit code, ${_ec} expanded
    assert f"shell:{handle.session_id}" in argv
    assert handle.output_path in argv  # --tail-file target

    # Default keep=False: the session closes unconditionally after the notice
    # (delivery is best-effort; a failed send must not leave the shell behind).
    deadline = time.time() + 10
    while time.time() < deadline and handle.session_id in ava.shell.list():
        time.sleep(0.3)
    assert handle.session_id not in ava.shell.list()


@pytest.mark.flaky  # real pty session + time.sleep polling (15s deadline)
def test_failed_cmd_closes_session_even_when_notice_fails(
    _agent_row: int, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Bug B regression (Task #1115): a failed command must close the session
    even when the completion notice cannot be delivered. A shell that outlives
    its command is what the watcher boot reconcile reads as "alive", so a dead
    watcher (crash / reaped python) is never rebuilt.

    The CLI is faked with a script that exits 1: under the old `&& exit` the
    close was gated on delivery and a failed notice left the session up; with
    the unconditional `; exit $_ec` the session must end regardless."""
    fake_cli = tmp_path / "fake-ava-fail"
    fake_cli.write_text("#!/bin/sh\nexit 1\n")
    fake_cli.chmod(0o755)
    monkeypatch.setattr(_background, "cli_path", lambda: fake_cli)  # pyright: ignore[reportUnknownArgumentType]

    handle = ava.shell.run_background("false", name="test-bg-fail", cwd=str(tmp_path))

    # Command failed AND the notice failed — the session must still close.
    deadline = time.time() + 15
    while time.time() < deadline and handle.session_id in ava.shell.list():
        time.sleep(0.3)
    assert handle.session_id not in ava.shell.list()


@pytest.mark.flaky  # real pty session + time.sleep polling (15s deadline)
def test_run_background_keep_leaves_session(
    _agent_row: int, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    argv_file = tmp_path / "argv.txt"
    fake_cli = tmp_path / "fake-ava"
    fake_cli.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {argv_file}\n")
    fake_cli.chmod(0o755)
    monkeypatch.setattr(_background, "cli_path", lambda: fake_cli)  # pyright: ignore[reportUnknownArgumentType]

    handle = ava.shell.run_background("echo done", name="test-keep", cwd=str(tmp_path), keep=True)
    try:
        deadline = time.time() + 15
        while time.time() < deadline and not argv_file.exists():
            time.sleep(0.3)
        assert argv_file.exists(), "completion notice never fired"
        time.sleep(0.5)  # give a hypothetical exit a beat to happen — it must not
        assert handle.session_id in ava.shell.list()
    finally:
        ava.shell.kill(handle.session_id)
