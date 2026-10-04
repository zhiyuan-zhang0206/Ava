"""A login shell that cannot start says why on its own terminal."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

from services.agent_runner.pty_sessions import session


def test_a_failed_shell_start_reports_its_cause_on_the_pty(tmp_path: Path) -> None:
    pid, master = session.fork_shell(str(tmp_path / "missing"), {}, 80, 24)
    out = b""
    try:
        with contextlib.suppress(OSError):  # Linux reports the slave's close as EIO
            while chunk := os.read(master, 4096):
                out += chunk
    finally:
        os.close(master)
    _, status = os.waitpid(pid, 0)

    assert os.WEXITSTATUS(status) == 127
    assert b"could not start the login shell" in out
    assert b"FileNotFoundError" in out
