"""Agent process identity — `ops/agent_identity.py`.

The unit under test answers "is this pid still that agent's process", which the
restarter's corpse reapers and the hibernation swap-out both turn on. Two things
are worth locking:

- the **verdicts**, especially that `UNREADABLE` is not `FOREIGN`: only the latter
  may drive a reap or block a signal, because only the latter rests on a cmdline
  that was actually read;
- the **drift guard** — `ops/agent_launch.py` must keep building an argv the
  matcher recognizes. Nothing at runtime would notice if it stopped: the probe
  would silently start calling every live agent a stranger, and the reaper would
  terminate the fleet. So the real launcher is driven here (fake supervisor, the
  `test_no_secrets_on_argv` pattern) and its argv fed back through the matcher.

The two "live stranger" cases use a real subprocess rather than a stub: the whole
defect (issue #1123) was that a pid can be alive *and* not ours, and that is
exactly the state a stub cannot vouch for.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from ops.agent_identity import (
    AgentProcessIdentity,
    cmdline_identifies_agent,
    probe_agent_process,
)
from shared.proc import process_cmdline


@pytest.fixture
def stranger_pid() -> Iterator[int]:
    """A real, live process that is emphatically not an agent — a stand-in for
    whatever the OS handed a recycled pid to."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        yield proc.pid
    finally:
        proc.kill()
        proc.wait()


class TestCmdlineMatch:
    def test_matches_the_agents_own_argv(self) -> None:
        argv = [
            "/x/.venv/bin/python",
            "-m",
            "agent",
            "--agent-id",
            "235",
            "--boot-stall-seconds",
            "60",
        ]
        assert cmdline_identifies_agent(argv, 235) is True

    def test_rejects_a_different_agents_argv(self) -> None:
        """The identity is per-agent, not per-fleet: agent 236's process holding a
        pid that agent 235's row still names is as foreign as any stranger."""
        argv = ["/x/.venv/bin/python", "-m", "agent", "--agent-id", "236"]
        assert cmdline_identifies_agent(argv, 235) is False

    def test_rejects_the_id_appearing_as_another_flags_value(self) -> None:
        """Why the fragments are matched as consecutive runs: agent 1's argv
        carries the token '235' as a boot window, and loose membership of
        '--agent-id' + '235' would read it as agent 235."""
        argv = [
            "/x/.venv/bin/python",
            "-m",
            "agent",
            "--agent-id",
            "1",
            "--boot-stall-seconds",
            "235",
        ]
        assert cmdline_identifies_agent(argv, 235) is False

    def test_rejects_a_non_agent_python_argv(self) -> None:
        """`--agent-id` alone is not enough — an ops command could carry it."""
        assert cmdline_identifies_agent(["python", "-m", "cli.main", "--agent-id", "235"], 235) is (
            False
        )

    def test_tolerates_argv_shorter_than_the_matched_run(self) -> None:
        assert cmdline_identifies_agent([], 235) is False
        assert cmdline_identifies_agent(["python"], 235) is False


class TestProbeVerdicts:
    def test_owned_when_cmdline_is_this_agents(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "ops.agent_identity.process_cmdline",
            lambda _pid: ["python", "-m", "agent", "--agent-id", "7"],  # pyright: ignore[reportUnknownArgumentType]
        )
        assert probe_agent_process(4242, 7) is AgentProcessIdentity.OWNED

    def test_foreign_for_a_real_live_stranger(self, stranger_pid: int) -> None:
        """The prod failure state, unmocked: a pid that is alive (so the old
        `process_alive` check passed it) and belongs to something unrelated."""
        assert probe_agent_process(stranger_pid, 7) is AgentProcessIdentity.FOREIGN

    def test_gone_when_the_pid_is_not_alive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("ops.agent_identity.process_cmdline", lambda _pid: None)  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr("ops.agent_identity.process_alive", lambda _pid: False)  # pyright: ignore[reportUnknownArgumentType]
        assert probe_agent_process(4242, 7) is AgentProcessIdentity.GONE

    def test_unreadable_when_alive_but_argv_is_not_readable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Another user's process, or a zombie. Must NOT collapse into FOREIGN:
        the callers reap on FOREIGN, and no evidence is not evidence."""
        monkeypatch.setattr("ops.agent_identity.process_cmdline", lambda _pid: None)  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr("ops.agent_identity.process_alive", lambda _pid: True)  # pyright: ignore[reportUnknownArgumentType]
        assert probe_agent_process(4242, 7) is AgentProcessIdentity.UNREADABLE


class TestProcessCmdline:
    def test_reads_a_live_processes_argv(self, stranger_pid: int) -> None:
        cmdline = process_cmdline(stranger_pid)
        assert cmdline is not None
        assert "import time; time.sleep(60)" in cmdline

    def test_reads_this_process(self) -> None:
        assert process_cmdline(os.getpid())

    def test_none_for_a_reaped_process(self) -> None:
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        assert process_cmdline(proc.pid) is None


@pytest.mark.real_agent_launch
def test_launcher_argv_is_recognized_by_the_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """The drift guard. `_launch_agent_process` and the matcher share
    `ops.agent_identity`'s fragments; this drives the real launcher end to end so
    that a future argv rewrite which stops using them fails HERE, loudly, instead
    of in prod as a fleet-wide reap.

    Supervisor faked the same way `tests/shared/test_no_secrets_on_argv.py` does —
    the agent launch double-forks through `shared._reparent`, so there is no
    `subprocess.run` to intercept.
    """
    from ops.agent_launch import _launch_agent_process

    captured: list[list[str]] = []

    class _FakeSupervisor:
        @staticmethod
        def new_session(_name: str, argv: list[str], _cwd: Path, **_kw: Any) -> bool:
            captured.append(list(argv))
            return True

        @staticmethod
        def kill_session(*_a: Any, **_kw: Any) -> tuple[bool, str]:
            return (True, "noop")

    monkeypatch.setattr("ops.agent_launch.native_proc", lambda: _FakeSupervisor)
    monkeypatch.setattr("ops.agent_launch._wait_for_agent_claim", lambda _id: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("ops.agent_launch.agent_spawn_env_dict", dict)

    _launch_agent_process(1123)

    argv = captured[0]
    assert cmdline_identifies_agent(argv, 1123) is True
    assert cmdline_identifies_agent(argv, 1124) is False
