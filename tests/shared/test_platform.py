"""Unit tests for shared.platform — host detection + the disk-path probe.

The WSL-marker and primary-disk-path logic used to live in the retired
shared.resource_monitor (as `_is_wsl` / `_disk_usage_path`); it now lives here as the canonical
`_detect_wsl` / `primary_disk_path`, so these tests followed it.
"""

from __future__ import annotations

import io
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import shared.platform as plat
from shared.platform import (
    _detect_wsl,
    ensure_line_buffered_stdio,
    primary_disk_path,
    pty_max,
)


class TestDetectWsl:
    @pytest.mark.parametrize(
        "release",
        [
            "6.18.33.1-microsoft-standard-WSL2",  # WSL2
            "4.4.0-19041-Microsoft",  # WSL1
            "5.15.0-custom-WSL",
        ],
    )
    def test_wsl_kernels_detected(self, release: str) -> None:
        assert _detect_wsl(release) is True

    @pytest.mark.parametrize("release", ["5.15.0-91-generic", "23.2.0", "6.1.0-amd64"])
    def test_non_wsl_not_detected(self, release: str) -> None:
        assert _detect_wsl(release) is False


class TestPrimaryDiskPath:
    def test_macos_data_volume(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(plat, "IS_MACOS", True)
        assert primary_disk_path() == "/System/Volumes/Data"

    def test_wsl_uses_ext4_rootfs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # WSL samples its own ext4 rootfs, not the auto-mounted Windows /mnt/c
        # (whose near-full C: drive has nothing to do with this Linux machine).
        monkeypatch.setattr(plat, "IS_MACOS", False)
        monkeypatch.setattr(plat, "IS_WSL", True)
        monkeypatch.setattr(plat, "IS_WINDOWS", False)
        assert primary_disk_path() == "/"

    def test_windows_system_drive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(plat, "IS_MACOS", False)
        monkeypatch.setattr(plat, "IS_WSL", False)
        monkeypatch.setattr(plat, "IS_WINDOWS", True)
        assert primary_disk_path() == "C:\\"

    def test_plain_posix_root(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(plat, "IS_MACOS", False)
        monkeypatch.setattr(plat, "IS_WSL", False)
        monkeypatch.setattr(plat, "IS_WINDOWS", False)
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
        repo_root = str(Path(__file__).resolve().parents[2])
        script = (
            f"import sys, time\n"
            f"sys.path.insert(0, {repo_root!r})\n"
            f"from shared.platform import ensure_line_buffered_stdio\n"
            f"ensure_line_buffered_stdio()\n"
            f"print('[ava cluster update] header')\n"
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
        assert got[0].strip() == "[ava cluster update] header"

    def test_idempotent_and_survives_a_stream_without_reconfigure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Buffering is a nicety: a replaced stdout (pytest capture, a StringIO under
        redirect_stdout) must never take the command down."""
        monkeypatch.setattr(sys, "stdout", io.StringIO())
        ensure_line_buffered_stdio()
        ensure_line_buffered_stdio()
