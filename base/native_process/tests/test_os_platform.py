"""Unit tests for base.native_process.os_platform — host detection + the disk-path probe.

The primary-disk-path probe samples the macOS data volume or the POSIX root,
including a WSL process's own root filesystem.
"""

from __future__ import annotations

import io
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import base.native_process.os_platform as plat
from base.native_process.os_platform import (
    descends_from_launchd_job,
    ensure_line_buffered_stdio,
    launchd_job_loaded,
    primary_disk_path,
    pty_max,
)


def test_cold_import_does_not_probe_uname() -> None:
    result = subprocess.run(
        [
            ".venv/bin/python",
            "-I",
            "-c",
            """
import platform
import sys
import uuid

# Linux's stdlib UUID initialization probes the OS independently of Ava.
assert "base.native_process" not in sys.modules
assert "base.native_process.os_platform" not in sys.modules

def refuse_probe():
    raise AssertionError("platform import must not probe uname")

platform.uname = refuse_probe
import base.native_process.os_platform
""",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr


class TestPrimaryDiskPath:
    def test_macos_data_volume(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(plat, "IS_MACOS", True)
        assert primary_disk_path() == "/System/Volumes/Data"

    def test_plain_posix_root(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(plat, "IS_MACOS", False)
        assert primary_disk_path() == "/"


class TestPtyMax:
    def test_non_macos_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Off macOS the PTY ceiling does not bind (Linux `kernel.pty.max` is far
        higher), so callers get None and skip the check."""
        monkeypatch.setattr(plat, "IS_MACOS", False)
        assert pty_max() is None

    @pytest.mark.skipif(not plat.IS_MACOS, reason="reads kern.tty.ptmx_max, macOS-only")
    def test_macos_reads_positive_ceiling(self) -> None:
        """On macOS it returns the live `kern.tty.ptmx_max` — a positive int
        (511 by default)."""
        value = pty_max()
        assert isinstance(value, int)
        assert value > 0


class TestEnsureLineBufferedStdio:
    def test_pipe_stdout_streams_before_exit(self) -> None:
        """The regression: a CLI whose stdout is a PIPE block-buffers its own
        `print()` output, so a detached `ava ... | tee` log shows the parent's lines
        only at exit — after the unbuffered output of every child it spawned. Run a
        child that prints and then blocks, and assert the line is readable while it
        is still alive."""
        repo_root = str(Path(__file__).resolve().parents[3])
        script = (
            f"import sys, time\n"
            f"sys.path.insert(0, {repo_root!r})\n"
            f"from base.native_process.os_platform import ensure_line_buffered_stdio\n"
            f"ensure_line_buffered_stdio()\n"
            f"print('[update] header')\n"
            f"time.sleep(30)\n"
        )
        proc = subprocess.Popen(  # noqa: S603 — this interpreter, a literal script
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            text=True,
        )
        # The read must be TIME-bounded, not just eventual: without the fix the line
        # does arrive — at exit, 30s later, which is exactly the bug (the parent's
        # lines land after everything its children streamed). A plain blocking
        # readline would therefore pass on the broken build, just slowly.
        assert proc.stdout is not None
        got: list[str] = []
        reader = threading.Thread(target=lambda: got.append(proc.stdout.readline()))  # type: ignore[union-attr]
        reader.daemon = True
        reader.start()
        reader.join(timeout=10.0)
        proc.kill()
        proc.wait(timeout=10)
        assert got, "the child's line did not reach the pipe while it was still running"
        assert got[0].strip() == "[update] header"

    def test_idempotent_and_survives_a_stream_without_reconfigure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Buffering is a nicety: a replaced stdout (pytest capture, a StringIO under
        redirect_stdout) must never take the command down."""
        monkeypatch.setattr(sys, "stdout", io.StringIO())
        ensure_line_buffered_stdio()
        ensure_line_buffered_stdio()


class TestLaunchdOwnership:
    """`launchd_job_loaded` / `descends_from_launchd_job` — the ownership checks
    behind the self-reload guards (`base/host/system/cron`, `base/os_watchdog_probe`).

    The inherited `XPC_SERVICE_NAME` is not trustworthy: only the job's direct
    child reads the label, every exec'd descendant reads "0" (2026-09-17,
    docs/postmortems/0008). So the tree check asks launchd for the job's pid and
    walks this process's ancestry with ps.
    """

    def _fake(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        print_rc: int,
        print_out: str,
        ps: dict[int, str],
    ) -> None:
        def run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            if cmd[1] == "print":
                return subprocess.CompletedProcess(cmd, print_rc, print_out, "")
            value = ps.get(int(cmd[-1]), "")
            return subprocess.CompletedProcess(cmd, 0 if value else 1, value, "")

        monkeypatch.setattr(plat.subprocess, "run", run)
        monkeypatch.setattr(plat, "IS_MACOS", True)
        monkeypatch.setattr(plat.os, "getpid", lambda: 500)

    def test_off_macos_is_never_owned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(plat, "IS_MACOS", False)
        assert descends_from_launchd_job("com.x") is False
        assert launchd_job_loaded("com.x") is False

    def test_loaded_follows_launchctl_verdict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._fake(monkeypatch, print_rc=0, print_out="", ps={})
        assert launchd_job_loaded("com.x") is True
        self._fake(monkeypatch, print_rc=113, print_out="", ps={})
        assert launchd_job_loaded("com.x") is False

    def test_unloaded_job_owns_no_process(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._fake(monkeypatch, print_rc=113, print_out="Could not find service", ps={})
        assert descends_from_launchd_job("com.x") is False

    def test_loaded_but_not_running_owns_no_process(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._fake(monkeypatch, print_rc=0, print_out="state = not running\n", ps={})
        assert descends_from_launchd_job("com.x") is False

    def test_ancestor_chain_proves_ownership(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._fake(monkeypatch, print_rc=0, print_out="\tpid = 200\n", ps={500: "300", 300: "200"})
        assert descends_from_launchd_job("com.x") is True

    def test_chain_reaching_launchd_is_external(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._fake(monkeypatch, print_rc=0, print_out="pid = 999\n", ps={500: "300", 300: "1"})
        assert descends_from_launchd_job("com.x") is False

    def test_unreadable_ancestor_is_not_proof(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._fake(monkeypatch, print_rc=0, print_out="pid = 200\n", ps={500: "300"})
        assert descends_from_launchd_job("com.x") is False
