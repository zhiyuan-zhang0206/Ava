"""The daemon-start ghost reap survives a process-table read failure.

`_reap_stale_daemons` walks psutil's process table before binding; a cmdline
read that raises (psutil 7.2.2 on macOS raised SystemError mid-iteration) must
skip that process, never the reap — ghosts of earlier respawn storms must not
survive because one unrelated process was unreadable (task #4964).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import ava.mcps._daemon as daemon_mod
from base.host.env.dotenv_boot import resolve_ava_home


class _FakeProc:
    """A psutil.Process stand-in for the reaper: argv, cwd, env, and a kill flag."""

    def __init__(self, pid: int, cmdline: list[str], cwd: str, env: dict[str, str]) -> None:
        self.pid = pid
        self._cmdline = cmdline
        self._cwd = cwd
        self._env = env
        self.killed = False

    def cmdline(self) -> list[str]:
        return self._cmdline

    def cwd(self) -> str:
        return self._cwd

    def environ(self) -> dict[str, str]:
        return self._env

    def kill(self) -> None:
        self.killed = True


class _ExplodingProc:
    """A process whose cmdline read raises the way psutil 7.2.2 did on macOS
    (SystemError mid-walk) — through both the attrs shape and the direct read,
    so a regression to `process_iter(attrs)` stays caught."""

    pid = 424242
    killed = False

    @property
    def info(self) -> dict[str, object]:
        raise SystemError("psutil: cmdline read failed mid-iteration")

    def cmdline(self) -> list[str]:
        raise SystemError("psutil: cmdline read failed mid-iteration")


def test_reap_stale_daemons_skips_a_process_whose_cmdline_read_fails() -> None:
    """A cmdline read that raises skips that process, not the reap: the other
    ghosts are still killed."""
    home = str(resolve_ava_home())
    root = str(Path(daemon_mod.__file__).resolve().parents[2])

    argv = [".venv/bin/python", "-m", "ava.mcps._daemon"]
    procs = [
        _ExplodingProc(),
        _FakeProc(3001, argv, root, {"AVA_HOME": home}),
        _FakeProc(3002, argv, "/elsewhere", {"AVA_HOME": home}),
    ]
    with patch("psutil.process_iter", return_value=procs):
        daemon_mod._reap_stale_daemons(Path(root))

    assert procs[1].killed and procs[2].killed
    assert not procs[0].killed
