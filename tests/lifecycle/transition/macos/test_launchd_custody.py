"""Job-group closure against real process groups (no launchd job is involved).

A leader process stands in for the finite helper: it creates a process group,
starts an "executor" and a same-group child, then exits and is reaped, exactly
the state launchd leaves after the helper dies and its SIGTERM-only group
cleanup has run. Every process is cleaned by its exact captured birth.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import psutil
import pytest

from cli.release_transition import launchd_custody as custody
from cli.release_transition.launchd_custody import Birth, GroupReceipt
from shared.native_process.ownership import OwnedProcess
from tests.lifecycle.transition.macos.launchd_fake import (
    HELPER_BIRTH,
    Harness,
    write_group_receipt,
)
from tests.lifecycle.transition.macos.launchd_fake import harness as harness

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")

_MEMBER = (
    "import signal, sys, time\n"
    "if sys.argv[1] == 'ignore': signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "open(sys.argv[2], 'w').write('ready')\n"
    "time.sleep(60)\n"
)
_LEADER = (
    "import json, subprocess, sys, time\n"
    "from pathlib import Path\n"
    "d = Path(sys.argv[1])\n"
    "pids = {}\n"
    "for name, mode in (('executor', sys.argv[2]), ('child', 'ignore')):\n"
    "    pids[name] = subprocess.Popen([sys.executable, '-c', sys.argv[3], mode,"
    " str(d / name)]).pid\n"
    "while not all((d / name).exists() for name in pids): time.sleep(0.01)\n"
    "(d / 'pids.json').write_text(json.dumps(pids))\n"
    "while not (d / 'release').exists(): time.sleep(0.01)\n"
)


class Group:
    def __init__(self, leader: OwnedProcess, executor: OwnedProcess, child: OwnedProcess) -> None:
        self.leader, self.executor, self.child = leader, executor, child

    @property
    def births(self) -> dict[str, Birth]:
        return {"helper": Birth.of(self.leader), "executor": Birth.of(self.executor)}


GroupFactory = Callable[[str], Group]


def _capture(pid: int) -> OwnedProcess:
    return OwnedProcess.capture(psutil.Process(pid))


@pytest.fixture
def group_factory(tmp_path: Path) -> Iterator[GroupFactory]:
    started: list[OwnedProcess] = []

    def start(executor_mode: str) -> Group:
        directory = tmp_path / f"group-{len(started)}"
        directory.mkdir()
        leader = subprocess.Popen(  # noqa: S603 — this interpreter and fixed code
            [sys.executable, "-c", _LEADER, str(directory), executor_mode, _MEMBER],
            start_new_session=True,
        )
        started.append(_capture(leader.pid))
        deadline = time.monotonic() + 20
        while not (directory / "pids.json").exists():
            assert time.monotonic() < deadline, "fixture group did not start"
            time.sleep(0.01)
        pids = json.loads((directory / "pids.json").read_text())
        executor, child = _capture(pids["executor"]), _capture(pids["child"])
        started.extend([executor, child])
        assert os.getpgid(executor.pid) == os.getpgid(child.pid) == leader.pid
        (directory / "release").touch()
        leader.wait(10)
        # launchd's cleanup after the helper exits: one SIGTERM to the group.
        os.killpg(leader.pid, signal.SIGTERM)
        return Group(started[-3], executor, child)

    yield start
    for birth in started:
        if birth.live():
            birth.send_signal(signal.SIGKILL)
    for birth in started:
        deadline = time.monotonic() + 10
        while birth.live() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not birth.live(), f"fixture process {birth.pid} survived cleanup"


def _gone(process: OwnedProcess, timeout: float = 5) -> bool:
    deadline = time.monotonic() + timeout
    while process.live():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    return True


@pytest.fixture(autouse=True)
def _short_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(custody, "_CLOSURE_WAIT_S", 0.5)
    monkeypatch.setattr(custody, "_KILL_WAIT_S", 3.0)


def test_term_ignoring_member_without_a_live_owner_is_refused_and_never_signalled(
    group_factory: GroupFactory,
) -> None:
    """Review P1-2/P2-3: a survivor of launchd's SIGTERM keeps custody, with evidence."""
    group = group_factory("default")
    assert _gone(group.executor), "default-disposition executor survived SIGTERM"
    assert group.child.live()
    for births in (group.births, None):
        with pytest.raises(RuntimeError, match="still has live group members") as refused:
            custody.prove_group_closed(group.leader.pid, births)
        message = str(refused.value)
        assert f"pid {group.child.pid}" in message
        assert f"process group {group.leader.pid}" in message and "kill -KILL" in message
        # Membership cannot be proven once no recorded owner pins the group.
        assert group.child.live()


def test_live_recorded_executor_pins_the_group_for_bounded_exact_kills(
    group_factory: GroupFactory,
) -> None:
    group = group_factory("ignore")
    assert group.executor.live() and group.child.live()
    custody.prove_group_closed(group.leader.pid, group.births)
    assert _gone(group.child) and _gone(group.executor)
    assert custody.group_empty(group.leader.pid, Birth.of(group.leader))


def test_empty_group_and_closed_births_close_immediately(group_factory: GroupFactory) -> None:
    group = group_factory("default")
    assert group.child.send_signal(signal.SIGKILL)
    assert _gone(group.child) and _gone(group.executor)
    deadline = time.monotonic() + 5
    while not custody.group_empty(group.leader.pid, None):
        assert time.monotonic() < deadline
        time.sleep(0.02)
    custody.prove_group_closed(group.leader.pid, group.births)


def test_reused_leader_pid_proves_the_original_group_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """XNU never gives a live group's id to a new process; a new birth there is proof."""
    reused = os.getpid()
    current = _capture(reused)
    monkeypatch.setattr(os, "killpg", lambda _pgid, _signal: None)
    earlier = None if current.starttime is None else current.starttime - 1
    helper = Birth(pid=reused, birth=current.birth - 1.0, starttime=earlier)
    assert not helper.owned().live()
    assert custody.group_empty(reused, helper)
    # Without the recorded helper birth the process at that PID may be the helper.
    assert not custody.group_empty(reused, None)
    assert not custody.group_empty(reused, Birth.of(current))


def _receipt(harness: Harness) -> Path:
    write_group_receipt(harness.launch, HELPER_BIRTH.pid)
    return Path(harness.launch.group_receipt)


def test_group_receipt_is_private_whole_and_led_by_the_helper(harness: Harness) -> None:
    path = _receipt(harness)
    assert custody.read_group_receipt(harness.launch) == GroupReceipt(
        finite_executor="v1", helper_pid=HELPER_BIRTH.pid, pgid=HELPER_BIRTH.pid, asid=100023
    )
    path.chmod(0o644)
    with pytest.raises(RuntimeError, match="not a private file"):
        custody.read_group_receipt(harness.launch)
    path.chmod(0o600)
    path.write_text('{"finite_executor": "v1", "helper_pid": 900')
    with pytest.raises(RuntimeError, match="unreadable"):
        custody.read_group_receipt(harness.launch)
    write_group_receipt(harness.launch, HELPER_BIRTH.pid, pgid=HELPER_BIRTH.pid + 1)
    with pytest.raises(RuntimeError, match="unreadable"):
        custody.read_group_receipt(harness.launch)
    path.unlink()
    target = path.with_name("elsewhere.json")
    target.write_text("{}")
    path.symlink_to(target)
    with pytest.raises(RuntimeError, match="not a private file"):
        custody.read_group_receipt(harness.launch)
    path.unlink()
    assert custody.read_group_receipt(harness.launch) is None
