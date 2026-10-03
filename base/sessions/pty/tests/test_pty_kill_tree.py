"""A PTY session kill takes the session's whole membership: real service, real shells.

Membership is the shell, its descendants, and every process in the shell's
POSIX session (`base/sessions/pty/session_tree.py`). Job control gives a
background job its own process group, so a `cmd &` job, and anything that
double-forked out of the shell's tree, is still taken by `client.kill`, which
the service answers with the kill's verdict.

Every process a test starts carries its private tmp dir on argv or is pinned
through `pty_reaper`, so a failing kill never leaks a process past the test.
"""

from __future__ import annotations

import contextlib
import os
import shlex
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import psutil
import pytest

from base.native_process.os_platform import IS_WINDOWS
from base.native_process.ownership import OwnedProcess
from base.sessions.pty import client
from tests.path_scoped.pty_reaper import PtyReaper
from tests.path_scoped.pty_reaper import pty_reaper as pty_reaper
from tests.path_scoped.pty_service import pty_service as pty_service
from tests.path_scoped.pty_shells import gone, new, output_until, type_line, wait_for

pytestmark = [
    pytest.mark.skipif(IS_WINDOWS, reason="pty sessions are POSIX-only"),
    pytest.mark.usefixtures("pty_service"),
]

_REPO = Path(__file__).resolve().parents[4]

_SLEEPER = """\
import signal, sys, time
if sys.argv[2:] == ["ignore-term"]:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
open(sys.argv[1] + ".tmp", "w").write(str(__import__("os").getpid()))
__import__("os").rename(sys.argv[1] + ".tmp", sys.argv[1])
time.sleep(300)
"""

# Double fork: the grandchild is reparented to init and leaves the shell's
# tree, but it stays in the shell's session (and in the exited job's group).
_DOUBLE_FORK = """\
import os, sys, time
if os.fork() == 0:
    if os.fork() == 0:
        open(sys.argv[1] + ".tmp", "w").write(str(os.getpid()))
        os.rename(sys.argv[1] + ".tmp", sys.argv[1])
        time.sleep(300)
        os._exit(0)
    os._exit(0)
os.wait()
"""

# A child that leaves the session (setsid) but stays in the shell's tree.
_SETSID_CHILD = """\
import os, sys, time
child = os.fork()
if child == 0:
    os.setsid()
    open(sys.argv[1] + ".tmp", "w").write(str(os.getpid()))
    os.rename(sys.argv[1] + ".tmp", sys.argv[1])
    time.sleep(300)
    os._exit(0)
os.waitpid(child, 0)
"""

# Ava's sovereign launch from inside a shell (what `ava start` does for its
# services): `base._reparent` setsids and forks the target out to init.
_SOVEREIGN_LAUNCH = """\
import subprocess, sys
repo, log, pidfile, marker = sys.argv[1:5]
helper = subprocess.run(
    [sys.executable, "-m", "base._reparent", log, log,
     sys.executable, "-c", "import time; time.sleep(300)", marker],
    cwd=repo, check=True, capture_output=True, text=True, timeout=30,
)
open(pidfile, "w").write(helper.stdout.strip())
"""

# A job that keeps spawning children: the kill must freeze it before it can
# add one the capture did not see.
_RESPAWNER = """\
import subprocess, sys, time
marker, ready = sys.argv[1:3]
children = []
while True:
    children.append(subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)", marker]))
    if len(children) == 3:
        open(ready, "w").close()
    time.sleep(0.05)
"""


def _script(home: Path, name: str, body: str) -> Path:
    path = home / name
    path.write_text(body, encoding="utf-8")
    return path


def _command(*argv: str | Path) -> str:
    return shlex.join([sys.executable, *(str(arg) for arg in argv)])


def _start(home: Path, name: str, reaper: PtyReaper) -> OwnedProcess:
    """A ready session whose shell the reaper holds."""
    new(name, home)
    shell = reaper.track_session(name)
    type_line(name, "echo kill-tree-ready")
    output_until(name, "kill-tree-ready")
    return shell


def _pid_from(pidfile: Path, reaper: PtyReaper) -> psutil.Process:
    assert wait_for(pidfile.exists), f"{pidfile.name} was never written"
    process = psutil.Process(int(pidfile.read_text(encoding="utf-8")))
    reaper.track(process)
    return process


def _in_tree(shell: OwnedProcess, pid: int) -> bool:
    return pid in {child.pid for child in psutil.Process(shell.pid).children(recursive=True)}


def _gone(process: psutil.Process) -> Callable[[], bool]:
    return lambda: gone(process)


def _running(process: psutil.Process) -> bool:
    return not gone(process) and process.is_running()


def _unrelated(home: Path) -> subprocess.Popen[bytes]:
    """A process this test starts outside every session."""
    marker = home / "unrelated"
    return subprocess.Popen(  # noqa: S603 — the test's own interpreter and literal argv
        [sys.executable, "-c", "import time; time.sleep(300)", str(marker)]
    )


def _stop(process: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(ProcessLookupError):
        process.kill()
    process.wait(timeout=10)


def test_kill_takes_a_background_job_and_spares_an_unrelated_process(
    unit_home: Path, pty_reaper: PtyReaper
) -> None:
    """`sleep 300 &` lives in its own process group; the kill still takes it."""
    name = "ava-test-tree-bg-1"
    shell = _start(unit_home, name, pty_reaper)
    unrelated = _unrelated(unit_home)
    try:
        type_line(name, "sleep 300 &")
        assert wait_for(lambda: bool(psutil.Process(shell.pid).children())), "job never started"
        (job,) = psutil.Process(shell.pid).children()
        pty_reaper.track(job)
        assert os.getpgid(job.pid) != shell.pid, "precondition: the job has its own group"

        verdict = client.kill(name, graceful=False)

        assert (verdict.mode, verdict.interrupted) == ("forced", True)
        assert wait_for(_gone(job)), "the background job survived the session kill"
        assert wait_for(lambda: not shell.live()), "the shell survived the session kill"
        assert _running(psutil.Process(unrelated.pid)), "an unrelated process was killed"
        assert not client.has_session(name)
        assert client.kill(name, graceful=False) == client.KillVerdict("noop", False), (
            "a second kill is a noop"
        )
    finally:
        _stop(unrelated)


def test_kill_takes_a_double_forked_orphan_and_reports_it_interrupted(
    unit_home: Path, pty_reaper: PtyReaper
) -> None:
    """A process that double-forked out of the shell's tree is still in the
    shell's session: it is running work (`interrupted`) and it dies."""
    name = "ava-test-tree-orphan-1"
    shell = _start(unit_home, name, pty_reaper)
    script = _script(unit_home, "double_fork.py", _DOUBLE_FORK)
    pidfile = unit_home / "orphan.pid"
    unrelated = _unrelated(unit_home)
    try:
        type_line(name, _command(script, pidfile))
        orphan = _pid_from(pidfile, pty_reaper)
        assert wait_for(lambda: not _in_tree(shell, orphan.pid)), "precondition: it left the tree"
        assert os.getsid(orphan.pid) == shell.pid, "precondition: it is in the shell's session"

        assert client.kill(name, graceful=False).interrupted is True

        assert wait_for(_gone(orphan)), "the double-forked orphan survived the session kill"
        assert _running(psutil.Process(unrelated.pid)), "an unrelated process was killed"
    finally:
        _stop(unrelated)


def test_kill_takes_a_setsid_child_still_in_the_tree(
    unit_home: Path, pty_reaper: PtyReaper
) -> None:
    """A child that left the session with setsid is still a descendant: it dies."""
    name = "ava-test-tree-setsid-1"
    shell = _start(unit_home, name, pty_reaper)
    script = _script(unit_home, "setsid_child.py", _SETSID_CHILD)
    pidfile = unit_home / "setsid.pid"
    type_line(name, _command(script, pidfile) + " &")
    child = _pid_from(pidfile, pty_reaper)
    assert os.getsid(child.pid) == child.pid, "precondition: it leads its own session"
    assert _in_tree(shell, child.pid), "precondition: it is still the shell's descendant"

    assert client.kill(name, graceful=False).interrupted is True

    assert wait_for(_gone(child)), "the setsid child survived the session kill"


def test_kill_spares_a_sovereign_reparented_launch(unit_home: Path, pty_reaper: PtyReaper) -> None:
    """setsid + reparent to init is how Ava launches what must outlive the shell
    that started it (`ava start` from an agent's shell): the kill spares it."""
    name = "ava-test-tree-sovereign-1"
    shell = _start(unit_home, name, pty_reaper)
    script = _script(unit_home, "sovereign.py", _SOVEREIGN_LAUNCH)
    pidfile = unit_home / "sovereign.pid"
    type_line(
        name,
        _command(script, _REPO, unit_home / "sovereign.log", pidfile, unit_home / "sovereign"),
    )
    sovereign = _pid_from(pidfile, pty_reaper)
    assert wait_for(lambda: not _in_tree(shell, sovereign.pid)), "precondition: it left the tree"
    assert os.getsid(sovereign.pid) != shell.pid, "precondition: it left the session"
    assert wait_for(lambda: not psutil.Process(shell.pid).children()), "the launcher never exited"

    assert client.kill(name, graceful=False).interrupted is False, "nothing but the shell was live"

    assert wait_for(lambda: not shell.live()), "the shell survived the session kill"
    assert _running(sovereign), "a sovereign launch died with the shell that started it"
    sovereign.kill()


def test_kill_freezes_a_job_that_keeps_spawning(unit_home: Path, pty_reaper: PtyReaper) -> None:
    """Nothing a spawning job forks during the kill is left behind."""
    name = "ava-test-tree-spawner-1"
    _start(unit_home, name, pty_reaper)
    marker = unit_home / "spawned"
    ready = unit_home / "spawner.ready"
    script = _script(unit_home, "respawner.py", _RESPAWNER)
    type_line(name, _command(script, marker, ready) + " &")
    assert wait_for(ready.exists), "the spawner never started"

    assert client.kill(name, graceful=False).interrupted is True

    def _left() -> list[int]:
        return [
            process.pid
            for process in psutil.process_iter(["cmdline"])
            if str(marker) in (process.info["cmdline"] or ()) and not gone(process)
        ]

    assert wait_for(lambda: not _left(), timeout=5.0), f"spawned processes survived: {_left()}"


def test_graceful_kill_takes_a_term_ignoring_background_job(
    unit_home: Path, pty_reaper: PtyReaper
) -> None:
    """A graceful kill TERMs first and still escalates onto the background job."""
    name = "ava-test-tree-graceful-1"
    _start(unit_home, name, pty_reaper)
    script = _script(unit_home, "sleeper.py", _SLEEPER)
    pidfile = unit_home / "stubborn.pid"
    type_line(name, _command(script, pidfile, "ignore-term") + " &")
    job = _pid_from(pidfile, pty_reaper)

    verdict = client.kill(name, graceful=True)

    assert verdict.interrupted is True
    assert wait_for(_gone(job)), "a TERM-ignoring background job survived the graceful kill"
    assert wait_for(lambda: not client.has_session(name))
