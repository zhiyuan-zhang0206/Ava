"""A PTY session kill takes the session's whole membership — real hosts, real shells.

Membership is the shell, its descendants, and every process in the shell's
POSIX session (`base/sessions/pty/session_tree.py`). Job control gives a
background job its own process group, so before this rule a `cmd &` job — and
anything that double-forked out of the shell's tree — survived every session
kill and could later wake an agent that was terminated with its sessions.

Every process a test starts carries its private tmp dir on argv or is pinned
through `pty_reaper`, so a failing kill never leaks a process past the test.
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import psutil
import pytest

from base.native_process import pid_starttime_ticks
from base.native_process.os_platform import IS_WINDOWS
from base.native_process.ownership import OwnedProcess, stable_create_time
from base.sessions.pty import cli as pty_cli
from base.sessions.pty import session_tree
from base.sessions.pty._paths import host_identity, record_path, socket_path
from base.sessions.pty.tests.test_pty_sessions_cli import (
    REPO,
    _has,
    _new,
    _output_until,
    _proc_exited,
    _run_cli,
    _send,
    _wait,
)
from tests.path_scoped.pty_reaper import PtyReaper
from tests.path_scoped.pty_reaper import pty_reaper as pty_reaper

pytestmark = pytest.mark.skipif(IS_WINDOWS, reason="pty sessions are POSIX-only")

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

# A stand-in session leader for the hostless-record sweep: a member in its own
# process group double-forks, so the survivor is in neither the leader's tree
# nor its group — only in its session.
_FAKE_SHELL = """\
import os, sys, time
member = os.fork()
if member == 0:
    os.setpgid(0, 0)
    if os.fork() == 0:
        open(sys.argv[1] + ".tmp", "w").write(str(os.getpid()))
        os.rename(sys.argv[1] + ".tmp", sys.argv[1])
        time.sleep(300)
        os._exit(0)
    os._exit(0)
os.waitpid(member, 0)
time.sleep(300)
"""


@pytest.fixture
def home(unit_home: Path, pty_reaper: PtyReaper) -> Path:
    """The test's unit home; `pty_reaper` tears down while it is still patched."""
    del pty_reaper
    return unit_home


def _script(home: Path, name: str, body: str) -> Path:
    path = home / name
    path.write_text(body, encoding="utf-8")
    return path


def _command(*argv: str | Path) -> str:
    return shlex.join([sys.executable, *(str(arg) for arg in argv)])


def _start(home: Path, name: str, reaper: PtyReaper) -> OwnedProcess:
    """A ready session whose host and shell the reaper holds."""
    _new(home, name)
    shell = reaper.track_session(name)
    _send(home, name, "echo kill-tree-ready")
    _output_until(home, name, "kill-tree-ready")
    return shell


def _pid_from(pidfile: Path, reaper: PtyReaper) -> psutil.Process:
    assert _wait(pidfile.exists), f"{pidfile.name} was never written"
    process = psutil.Process(int(pidfile.read_text(encoding="utf-8")))
    reaper.track(process)
    return process


def _in_tree(shell: OwnedProcess, pid: int) -> bool:
    return pid in {child.pid for child in psutil.Process(shell.pid).children(recursive=True)}


def _kill_verdict(home: Path, name: str, *, graceful: bool = False) -> str:
    result = _run_cli(home, name, "kill", *(["--graceful"] if graceful else []))
    assert result.returncode == 0, f"kill failed: {result.stderr}"
    return result.stdout.strip()


def _gone(process: psutil.Process) -> Callable[[], bool]:
    return lambda: _proc_exited(process)


def _running(process: psutil.Process) -> bool:
    return not _proc_exited(process) and process.is_running()


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
    home: Path, pty_reaper: PtyReaper
) -> None:
    """`sleep 300 &` lives in its own process group; the kill still takes it."""
    name = "ava-test-tree-bg-1"
    shell = _start(home, name, pty_reaper)
    unrelated = _unrelated(home)
    try:
        _send(home, name, "sleep 300 &")
        assert _wait(lambda: bool(psutil.Process(shell.pid).children())), "job never started"
        (job,) = psutil.Process(shell.pid).children()
        pty_reaper.track(job)
        assert os.getpgid(job.pid) != shell.pid, "precondition: the job has its own group"

        assert _kill_verdict(home, name) == "interrupted"

        assert _wait(_gone(job)), "the background job survived the session kill"
        assert _wait(lambda: not shell.live()), "the shell survived the session kill"
        assert _running(psutil.Process(unrelated.pid)), "an unrelated process was killed"
        assert not _has(home, name)
        assert _kill_verdict(home, name) == "idle", "a second kill is an idle noop"
    finally:
        _stop(unrelated)


def test_kill_takes_a_double_forked_orphan_and_reports_it_interrupted(
    home: Path, pty_reaper: PtyReaper
) -> None:
    """A process that double-forked out of the shell's tree is still in the
    shell's session: it is running work (`interrupted`) and it dies."""
    name = "ava-test-tree-orphan-1"
    shell = _start(home, name, pty_reaper)
    script = _script(home, "double_fork.py", _DOUBLE_FORK)
    pidfile = home / "orphan.pid"
    unrelated = _unrelated(home)
    try:
        _send(home, name, _command(script, pidfile))
        orphan = _pid_from(pidfile, pty_reaper)
        assert _wait(lambda: not _in_tree(shell, orphan.pid)), "precondition: it left the tree"
        assert os.getsid(orphan.pid) == shell.pid, "precondition: it is in the shell's session"

        assert _kill_verdict(home, name) == "interrupted"

        assert _wait(_gone(orphan)), "the double-forked orphan survived the session kill"
        assert _running(psutil.Process(unrelated.pid)), "an unrelated process was killed"
    finally:
        _stop(unrelated)


def test_kill_takes_a_setsid_child_still_in_the_tree(home: Path, pty_reaper: PtyReaper) -> None:
    """A child that left the session with setsid is still a descendant: it dies."""
    name = "ava-test-tree-setsid-1"
    shell = _start(home, name, pty_reaper)
    script = _script(home, "setsid_child.py", _SETSID_CHILD)
    pidfile = home / "setsid.pid"
    _send(home, name, _command(script, pidfile) + " &")
    child = _pid_from(pidfile, pty_reaper)
    assert os.getsid(child.pid) == child.pid, "precondition: it leads its own session"
    assert _in_tree(shell, child.pid), "precondition: it is still the shell's descendant"

    assert _kill_verdict(home, name) == "interrupted"

    assert _wait(_gone(child)), "the setsid child survived the session kill"


def test_kill_spares_a_sovereign_reparented_launch(home: Path, pty_reaper: PtyReaper) -> None:
    """setsid + reparent to init is how Ava launches what must outlive the shell
    that started it (`ava start` from an agent's shell): the kill spares it."""
    name = "ava-test-tree-sovereign-1"
    shell = _start(home, name, pty_reaper)
    script = _script(home, "sovereign.py", _SOVEREIGN_LAUNCH)
    pidfile = home / "sovereign.pid"
    _send(home, name, _command(script, REPO, home / "sovereign.log", pidfile, home / "sovereign"))
    sovereign = _pid_from(pidfile, pty_reaper)
    assert _wait(lambda: not _in_tree(shell, sovereign.pid)), "precondition: it left the tree"
    assert os.getsid(sovereign.pid) != shell.pid, "precondition: it left the session"
    assert _wait(lambda: not psutil.Process(shell.pid).children()), "the launcher never exited"

    assert _kill_verdict(home, name) == "idle"

    assert _wait(lambda: not shell.live()), "the shell survived the session kill"
    assert _running(sovereign), "a sovereign launch died with the shell that started it"
    sovereign.kill()


def test_kill_freezes_a_job_that_keeps_spawning(home: Path, pty_reaper: PtyReaper) -> None:
    """Nothing a spawning job forks during the kill is left behind."""
    name = "ava-test-tree-spawner-1"
    _start(home, name, pty_reaper)
    marker = home / "spawned"
    ready = home / "spawner.ready"
    script = _script(home, "respawner.py", _RESPAWNER)
    _send(home, name, _command(script, marker, ready) + " &")
    assert _wait(ready.exists), "the spawner never started"

    assert _kill_verdict(home, name) == "interrupted"

    def _left() -> list[int]:
        return [
            process.pid
            for process in psutil.process_iter(["cmdline"])
            if str(marker) in (process.info["cmdline"] or ()) and not _proc_exited(process)
        ]

    assert _wait(lambda: not _left(), timeout=5.0), f"spawned processes survived: {_left()}"


def test_graceful_kill_takes_a_term_ignoring_background_job(
    home: Path, pty_reaper: PtyReaper
) -> None:
    """A graceful kill TERMs first and still escalates onto the background job."""
    name = "ava-test-tree-graceful-1"
    _start(home, name, pty_reaper)
    script = _script(home, "sleeper.py", _SLEEPER)
    pidfile = home / "stubborn.pid"
    _send(home, name, _command(script, pidfile, "ignore-term") + " &")
    job = _pid_from(pidfile, pty_reaper)

    assert _kill_verdict(home, name, graceful=True) == "interrupted"

    assert _wait(_gone(job)), "a TERM-ignoring background job survived the graceful kill"


def test_record_kill_of_a_wedged_host_takes_the_background_job(
    home: Path, pty_reaper: PtyReaper
) -> None:
    """The CLI's record-based fallback (a host that stopped answering) kills
    the same membership the host would have."""
    name = "ava-test-tree-wedged-1"
    shell = _start(home, name, pty_reaper)
    identity = host_identity(record_path(name))
    assert identity is not None
    script = _script(home, "sleeper.py", _SLEEPER)
    pidfile = home / "job.pid"
    _send(home, name, _command(script, pidfile) + " &")
    job = _pid_from(pidfile, pty_reaper)
    os.kill(identity[0], signal.SIGSTOP)
    try:
        assert pty_cli._kill_by_record(name) == 0
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(identity[0], signal.SIGCONT)
    assert _wait(_gone(job)), "the background job survived the record-based kill"
    assert _wait(lambda: not shell.live()), "the shell survived the record-based kill"


def test_kill_of_a_recordless_host_takes_the_background_job(
    home: Path, pty_reaper: PtyReaper
) -> None:
    """The orphan-host reaper kills each host's sessions, not just its tree."""
    name = "ava-test-tree-recordless-1"
    shell = _start(home, name, pty_reaper)
    identity = host_identity(record_path(name))
    assert identity is not None
    host = psutil.Process(identity[0])
    script = _script(home, "double_fork.py", _DOUBLE_FORK)
    pidfile = home / "orphan.pid"
    _send(home, name, _command(script, pidfile))
    orphan = _pid_from(pidfile, pty_reaper)
    record_path(name).unlink()
    socket_path(name).unlink()

    assert _kill_verdict(home, name) == "idle"  # nothing answers: an absent session

    assert _wait(_gone(orphan)), "the orphan survived the recordless-host reap"
    assert _wait(lambda: not shell.live()), "the shell survived the recordless-host reap"
    assert _wait(_gone(host)), "the host survived its reap"


def test_hostless_record_sweep_kills_the_whole_session(home: Path, pty_reaper: PtyReaper) -> None:
    """A record whose host died is swept with its shell's whole session."""
    name = "ava-test-tree-hostless-1"
    script = _script(home, "fake_shell.py", _FAKE_SHELL)
    pidfile = home / "member.pid"
    leader = subprocess.Popen(  # noqa: S603 — the test's own interpreter and tmp script
        [sys.executable, str(script), str(pidfile)], start_new_session=True
    )
    try:
        pinned = psutil.Process(leader.pid)
        pty_reaper.track(pinned)
        member = _pid_from(pidfile, pty_reaper)
        assert os.getsid(member.pid) == leader.pid and os.getpgid(member.pid) != leader.pid
        raw = {
            "pid": leader.pid,
            "create_time": stable_create_time(pinned),
            "cmd": "fake shell",
            "cwd": str(home),
            "started_at": time.time(),
            "starttime": pid_starttime_ticks(leader.pid),
            "generation": None,
            "control_mode": None,
            "host_pid": 999999,
            "host_create_time": 0.0,
            "host_starttime": None,
        }
        record_path(name).parent.mkdir(parents=True, exist_ok=True)
        record_path(name).write_text(json.dumps(raw), encoding="utf-8")

        assert _run_cli(home, "list").stdout.strip() == ""

        assert _wait(_gone(member)), "the session member survived the hostless sweep"
        assert _wait(_gone(pinned)), "the orphan shell survived the hostless sweep"
    finally:
        _stop(leader)


@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded.*:DeprecationWarning")
def test_host_tree_kill_counts_a_zombie_host_as_reaped() -> None:
    """A dead-but-unreaped (zombie) host is not a survivor — the force-reap
    flake: is_running() stays True for zombies, so the reaper falsely reported
    "survived force-reap" after a kill (CI shard 4 flakes)."""
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        os.write(w, b"x")
        os._exit(0)
    os.close(w)
    try:
        os.read(r, 1)  # child has started its exit; parent has not waited
        proc = psutil.Process(pid)
        assert _wait(lambda: proc.status() == psutil.STATUS_ZOMBIE)
        assert session_tree.kill_host_tree(proc, wait_s=1.0) == session_tree.TreeKill((), ())
    finally:
        os.close(r)
        os.waitpid(pid, 0)  # reap so the test never leaks a zombie
