"""The shepherding binding of a maintenance hold (task #3270) -- shapes a/b/c.

The binding must die exactly when nobody is left to shepherd the ladder (agent
#405's ruling): a persistent session survives across its commands, a script
that drives the CLI directly is the root of the command tree, and a `sh -c`
relay between them must never be mistaken for the owner when it exits. These
tests build the three shapes with real processes.
"""

from __future__ import annotations

import contextlib
import json
import os
import select
import shlex
import signal
import subprocess
import sys
from pathlib import Path
from typing import cast

import psutil
import pytest

from shared.hold_driver import HoldDriver, ProcessRef, liveness, mint_driver
from shared.platform import IS_LINUX

_REPO = Path(__file__).resolve().parents[2]

_MINT = (
    "import json; from shared.hold_driver import mint_driver; "
    "print(json.dumps(mint_driver().encode(), sort_keys=True), flush=True)"
)

_RUN_MINT = (
    "p = subprocess.run([sys.executable, '-c', "
    + repr(_MINT)
    + "], capture_output=True, text=True)"
)
_PRINT = "print(p.stdout.strip(), flush=True)"


def _wrapper(body: list[str]) -> str:
    """A child program that runs `body`, then stays alive until killed."""
    return "\n".join(["import subprocess, sys, time", *body, "time.sleep(60)"])


def _spawn(source: str, *, new_session: bool) -> subprocess.Popen[str]:
    return subprocess.Popen(  # noqa: S603 -- test-controlled argv
        [sys.executable, "-c", source],
        cwd=_REPO,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=new_session,
    )


def _read_driver(proc: subprocess.Popen[str]) -> HoldDriver:
    assert proc.stdout is not None
    ready, _, _ = select.select([proc.stdout], [], [], 30.0)
    assert ready, "the minting child produced no output"
    line = proc.stdout.readline().strip()
    return HoldDriver.decode(json.loads(line))


def _reap(proc: subprocess.Popen[str]) -> None:
    with contextlib.suppress(ProcessLookupError, OSError):
        os.kill(proc.pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        proc.wait(timeout=10)


def test_a_persistent_session_shepherds_across_commands() -> None:
    """Shape a: the leader of a persistent session is the root, so the binding
    survives across the commands that session runs and dies with the session."""
    session = _spawn(
        _wrapper(
            [
                "for _ in range(2):",
                "    " + _RUN_MINT,
                "    " + _PRINT,
                "    time.sleep(0.2)",
            ]
        ),
        new_session=True,
    )
    first = second = None
    try:
        first = _read_driver(session)
        second = _read_driver(session)
        assert first.root is not None and first.root.pid == session.pid
        assert second.root is not None and second.root.pid == session.pid
        # The first minting command has already exited; the session still owns.
        assert liveness(first) == "alive"
        assert liveness(second) == "alive"
    finally:
        _reap(session)
    assert first is not None and liveness(first) == "dead"
    assert second is not None and liveness(second) == "dead"


def test_a_script_driving_the_cli_directly_is_the_root() -> None:
    """Shape b: the script that runs `subprocess` itself is the root, so its
    exit -- not the command's -- ends the binding (owner=script)."""
    script = _spawn(_wrapper([_RUN_MINT, _PRINT]), new_session=True)
    driver = None
    try:
        driver = _read_driver(script)
        assert driver.root is not None
        assert driver.root.pid == script.pid
        assert liveness(driver) == "alive"
    finally:
        _reap(script)
    assert driver is not None and liveness(driver) == "dead"


def test_without_a_readable_leader_the_direct_parent_is_the_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fallback rule itself (deterministic, mocked capture): when the
    session leader cannot be found in the ancestry -- no `getsid`, a dead
    leader, a reparented command -- the DIRECT PARENT is the recorded root,
    never a topmost shim."""
    from shared import hold_driver

    class _Proc:
        def __init__(self, pid: int) -> None:
            self.pid = pid

    ancestors = [_Proc(101), _Proc(50), _Proc(7)]
    captured: list[int] = []
    refused: object = object()

    def _record(proc: object) -> object:
        captured.append(cast("_Proc", proc).pid)
        return refused

    monkeypatch.setattr(hold_driver, "_capture", _record)
    planted = cast("list[psutil.Process]", ancestors)
    assert hold_driver._resolve_root(planted, 999, leader=None) is refused
    assert captured == [101]  # the leader is absent -> direct parent

    captured.clear()
    assert hold_driver._resolve_root(planted, 50, leader=None) is refused
    assert captured == [101]  # 50 is the leader; 101 is the topmost below it

    captured.clear()
    only_leader = [cast("psutil.Process", _Proc(50))]
    assert hold_driver._resolve_root(only_leader, 50, leader=cast("ProcessRef", refused)) is refused
    assert captured == []  # nothing below the leader -> the leader itself


def test_a_shell_relay_is_never_the_owner() -> None:
    """Shape c: the `sh -c` relay sits below the script; its exit between steps
    must not read as owner-lost (the script still shepherds)."""
    relay_cmd = f"{sys.executable} -c {shlex.quote(_MINT)}"
    script = _spawn(
        _wrapper(
            [
                "import shlex",
                "p = subprocess.run("
                + repr(relay_cmd)
                + ", shell=True, capture_output=True, text=True)",
                _PRINT,
            ]
        ),
        new_session=True,
    )
    driver = None
    try:
        driver = _read_driver(script)
        assert driver.root is not None
        assert driver.root.pid == script.pid
        assert liveness(driver) == "alive"
    finally:
        _reap(script)
    assert driver is not None and liveness(driver) == "dead"


def test_a_forked_relay_below_the_leader_keeps_the_script_as_the_root() -> None:
    """The Linux shape of the relay test: a compound command cannot be
    exec-replaced, so the `sh -c` relay stays resident below the script while
    the ladder runs. The binding must skip it -- a relay is never the owner --
    rather than read owner-lost at its exit."""
    relay_cmd = f"{sys.executable} -c {shlex.quote(_MINT)} ; :"
    script = _spawn(
        _wrapper(
            [
                "import shlex",
                "p = subprocess.run("
                + repr(relay_cmd)
                + ", shell=True, capture_output=True, text=True)",
                _PRINT,
            ]
        ),
        new_session=True,
    )
    driver = None
    try:
        driver = _read_driver(script)
        assert driver.root is not None
        assert driver.root.pid == script.pid
        assert liveness(driver) == "alive"
    finally:
        _reap(script)
    assert driver is not None and liveness(driver) == "dead"


def test_the_relay_predicate_reads_only_inline_command_shells() -> None:
    """`_is_relay_shim` decides the exclusion: an inline command (`-c`, `-lc`)
    is a one-shot relay; a script file, an interactive shell and non-shell
    commands are not."""
    from shared import hold_driver

    class _CmdProc:
        def __init__(self, argv: list[str]) -> None:
            self._argv = argv

        def cmdline(self) -> list[str]:
            return self._argv

    def relay(argv: list[str]) -> bool:
        return hold_driver._is_relay_shim(cast("psutil.Process", _CmdProc(argv)))

    assert relay(["/bin/sh", "-c", "ava maintenance prepare"])
    assert relay(["bash", "-lc", "ava start"])
    assert not relay(["bash", "drill.sh"])
    assert not relay([sys.executable, "-c", "print(1)"])
    assert not relay(["/bin/zsh", "-i"])


def test_minting_in_this_process_reads_alive() -> None:
    assert liveness(mint_driver()) == "alive"


def test_missing_and_dead_read_as_such() -> None:
    assert liveness(None) == "missing"
    assert liveness(HoldDriver()) == "missing"
    ghost = ProcessRef(pid=99999, birth=0.0, starttime=1, argv="ghost")
    assert liveness(HoldDriver(root=ghost)) == "dead"


@pytest.mark.skipif(not IS_LINUX, reason="Linux /proc start-time identity")
def test_probe_reads_a_vanished_entry_after_validation_as_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stop-race window in the hold probe: psutil validated the pid, then
    the raw read found no /proc entry because the process was reaped in
    between — "no such pid", the unambiguous gone (the 2026-09-20 wave-2
    class), never a release deferred as unreadable."""
    from shared import session_record

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    pid = child.pid
    starttime = session_record.pid_starttime_ticks(pid)
    assert starttime is not None
    ref = ProcessRef(pid=pid, birth=0.5, starttime=starttime, argv="")
    real_read = session_record.pid_starttime_ticks

    def reaping_read(reading_pid: int) -> int | None:
        if reading_pid == pid:
            os.kill(pid, signal.SIGKILL)
            child.wait()
        return real_read(reading_pid)

    monkeypatch.setattr(session_record, "pid_starttime_ticks", reaping_read)
    try:
        assert ref.probe() == "gone"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


@pytest.mark.skipif(not IS_LINUX, reason="Linux /proc start-time identity")
def test_probe_keeps_unreadable_when_a_present_process_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pid that still exists while its start time cannot be read is never a
    death license."""
    from shared import session_record

    ref = ProcessRef(pid=os.getpid(), birth=0.5, starttime=1, argv="")

    def unreadable_read(_pid: int) -> int | None:
        return None

    monkeypatch.setattr(session_record, "pid_starttime_ticks", unreadable_read)
    assert ref.probe() == "unreadable"
