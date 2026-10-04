"""Shell sessions are an agent's: a process without an agent identity is refused, never given a global."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from base.native_process.os_platform import IS_WINDOWS

_PROBE = (
    "import ava.shell.sessions as sessions\n"
    "try:\n"
    "    print('LIST=' + repr(sessions.list()))\n"
    "except RuntimeError as exc:\n"
    "    print('REFUSED=' + str(exc))\n"
)


def _run_probe(home: Path, *, agent_id: int | None) -> str:
    env = {k: v for k, v in os.environ.items() if k != "AVA_AGENT_ID"}
    env.update(AVA_HOME=str(home), AVA_CONFIG_FETCH="skip")
    if agent_id is not None:
        env["AVA_AGENT_ID"] = str(agent_id)
    result = subprocess.run(  # noqa: S603 — fixed interpreter and probe source
        [sys.executable, "-c", _PROBE],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip().splitlines()[-1]


@pytest.mark.skipif(IS_WINDOWS, reason="PTY sessions require POSIX")
def test_exec_child_without_identity_is_refused_before_any_backend_or_database(
    tmp_path: Path,
) -> None:
    line = _run_probe(tmp_path, agent_id=None)
    assert line.startswith("REFUSED=Cannot use shell sessions: this process has no agent identity")


@pytest.mark.skipif(IS_WINDOWS, reason="PTY sessions require POSIX")
def test_exec_child_with_the_identity_the_launcher_passes_reaches_the_backend(
    tmp_path: Path,
) -> None:
    assert _run_probe(tmp_path, agent_id=41) == "LIST={}"


class _CapturingBackend:
    """Records the env dict `create()` hands the session backend."""

    def __init__(self) -> None:
        self.env: dict[str, str] = {}

    def new_session(self, name: str, cmd: str, cwd: Path, *, env: dict[str, str]) -> bool:
        self.env = dict(env)
        return True

    def list_sessions(self) -> list[str]:
        return []


@pytest.mark.skipif(IS_WINDOWS, reason="PTY sessions require POSIX")
def test_created_session_carries_the_owners_identity(tmp_path: Path) -> None:
    """Every session names its owner: a script run in it constructs the owner's complete
    AvaContext from the environment (ruling 2026-10-04, source 2)."""
    from ava.shell.sessions import ShellSessions
    from ava.shell.tests.support import FakeDatabase

    backend = _CapturingBackend()
    sessions = ShellSessions(backend=backend, database=FakeDatabase(), agent_id=41)
    sessions.create(name="probe", cwd=str(tmp_path), ttl=120)
    assert backend.env["AVA_AGENT_ID"] == "41"
