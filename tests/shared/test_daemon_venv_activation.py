"""Regression: a daemon session must run with the checkout venv activated.

Daemons launch as `.venv/bin/python -m X` (an absolute-ish path that does not
consult PATH), but several shell out to a bare binary that DOES:
`services.milvus.daemon` execvp's `milvus-lite`, and daemons that run `ava` /
`python` rely on PATH. `shared.session_env.venv_activation_prefix` prepends the
venv bin onto PATH *inside* the session command, because the two things that
otherwise carry it do not survive:

- the launch path (`session_backend.new_session`) forwards VIRTUAL_ENV + venv
  PATH via the env dict, but the session runs under a login shell (`bash -lc`)
  whose profile / macOS `path_helper` rebuilds PATH and drops the prefix;
- the respawn path (`service_respawn.respawn_service`) forwards
  `forward_env_prefix`, which carries neither VIRTUAL_ENV nor PATH at all.

These spawn a REAL session on the platform backend (the login-shell PATH
rebuild is the whole point — a string-shape assertion would not catch it) and
read back the env the command actually sees.
"""

from __future__ import annotations

import shlex
import shutil
import time
from pathlib import Path

import pytest

from shared.paths import repo_root
from shared.platform import IS_WINDOWS

pytestmark = [
    pytest.mark.skipif(
        IS_WINDOWS or shutil.which("bash") is None,
        reason="needs a POSIX host with bash (the login-shell PATH rebuild is under test)",
    ),
    # A real session IS the subject here, so opt out of the autouse guard that
    # stubs respawn_service suite-wide (tests/conftest.py:_guard_service_respawn) —
    # under the stub these assertions would pass without spawning anything. The
    # session lands on this unit's own session home and each test kills its own.
    pytest.mark.real_service_respawn,
]

_EXPECTED_BIN = str(repo_root() / ".venv" / "bin")
_EXPECTED_VENV = str(repo_root() / ".venv")
_EXPECTED_TOOLCHAIN = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]


def _probe_cmd(out: Path) -> str:
    # Runs after `cd <repo> && <activation> && exec ` — captures the env the
    # daemon sees. Dumps the whole PATH; the first entry is split off in Python
    # to avoid shell parameter-expansion surviving the layers of shlex quoting.
    # A single command (`sh -c ...`), because a service session execs into its
    # command: the probe has to have the shape a real daemon command has, and
    # the inner sh inherits the exported PATH/VIRTUAL_ENV being measured.
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
    """`session_backend.new_session` (the `ava start` launcher) runs the command
    with the venv bin first on PATH, surviving the login shell."""
    from shared.platform import raise_fd_limit
    from shared.session_backend import get_backend
    from shared.session_env import forward_env_dict

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


def test_respawn_path_activates_venv(unit_home: Path) -> None:
    """`service_respawn.respawn_service` (the healthcheck respawn path) likewise
    runs with the venv activated, even though `forward_env_prefix` carries no PATH."""
    from shared.cluster import session_name
    from shared.service_respawn import respawn_service
    from shared.session_backend import get_backend

    out = unit_home / "respawn_env.txt"
    assert respawn_service("venvcheck-respawn", _probe_cmd(out), repo_root())
    try:
        env = _read(out)
        assert _path0(env) == _EXPECTED_BIN, env
        assert env.get("VE") == _EXPECTED_VENV, env
    finally:
        get_backend().kill_session(session_name("venvcheck-respawn"), graceful=False)
