from __future__ import annotations

import shlex
import shutil
import time
from pathlib import Path

import pytest

from base.paths import repo_root

pytestmark = [
    pytest.mark.skipif(
        shutil.which("bash") is None,
        reason="needs a host with bash (the login-shell PATH rebuild is under test)",
    ),
]

_EXPECTED_BIN = str(repo_root() / ".venv" / "bin")
_EXPECTED_VENV = str(repo_root() / ".venv")
_EXPECTED_TOOLCHAIN = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]


def _probe_cmd(out: Path) -> str:
    script = f'{{ printf "PATH=%s\\n" "$PATH"; printf "VE=%s\\n" "$VIRTUAL_ENV"; }} > {out}'
    return f"/bin/sh -c {shlex.quote(script)}"


def _read(out: Path, timeout: float = 8.0) -> dict[str, str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if out.exists() and out.read_text().count("\n") >= 2:
            break
        time.sleep(0.1)
    return dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)


def _path0(env: dict[str, str]) -> str:
    return env.get("PATH", "").split(":", 1)[0]


def test_launch_path_activates_venv(unit_home: Path) -> None:
    from base.native_process.os_platform import raise_fd_limit
    from base.sessions.backend import get_backend
    from base.sessions.env_forwarding import forward_env_dict

    raise_fd_limit(65536)
    backend = get_backend()
    out = unit_home / "launch_env.txt"
    assert backend.new_session(
        "venvcheck-launch", _probe_cmd(out), repo_root(), env=forward_env_dict()
    )
    try:
        env = _read(out)
        assert _path0(env) == _EXPECTED_BIN, env
        assert env["PATH"].split(":")[1:5] == _EXPECTED_TOOLCHAIN, env
        assert env.get("VE") == _EXPECTED_VENV, env
    finally:
        backend.kill_session("venvcheck-launch", graceful=False)
