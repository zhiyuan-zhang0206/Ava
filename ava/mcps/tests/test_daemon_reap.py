from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import ava.mcps._daemon as daemon_mod
from base.paths.home import resolve_ava_home


class _FakeProc:
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
    pid = 424242
    killed = False

    @property
    def info(self) -> dict[str, object]:
        raise SystemError("psutil: cmdline read failed mid-iteration")

    def cmdline(self) -> list[str]:
        raise SystemError("psutil: cmdline read failed mid-iteration")


def test_reap_stale_daemons_skips_a_process_whose_cmdline_read_fails() -> None:
    home = str(resolve_ava_home())
    root = str(Path(daemon_mod.__file__).resolve().parents[2])

    argv = [".venv/bin/python", "-m", "ava.mcps._daemon"]
    procs = [
        _ExplodingProc(),
        _FakeProc(3001, argv, root, {"AVA_HOME": home}),
        _FakeProc(3002, argv, "/elsewhere", {"AVA_HOME": home}),
    ]
    with patch("psutil.process_iter", return_value=procs):
        daemon_mod.reap_stale_daemons(Path(root))

    assert procs[1].killed and procs[2].killed
    assert not procs[0].killed
