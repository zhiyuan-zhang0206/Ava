"""The races around a PTY session kill (`base/sessions/pty/session_tree.py`).

Each test pins a window the #3521 review found, on real processes: a kill that
orphans a stopped process group and lets the kernel SIGCONT a frozen member; a
member that keeps forking while the kill runs; a kill that raises with members
still frozen; and a session id that only a live member can vouch for. The
service's own kill (`services/pty_sessions/session.py`) runs this same
`session_tree` core; its verdict for a member the caller may not signal is
pinned in `services/pty_sessions/tests/test_session.py`.

Every process a test starts carries the test's tmp dir on argv (it runs a
script from there), so `pty_reaper` reaps whatever a failing kill leaves.
"""

from __future__ import annotations

import functools
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import psutil
import pytest

from base.native_process.os_platform import IS_WINDOWS
from base.native_process.ownership import OwnedProcess, shown_name
from base.sessions.pty import session_tree
from tests.path_scoped.pty_reaper import PtyReaper
from tests.path_scoped.pty_reaper import pty_reaper as pty_reaper

pytestmark = pytest.mark.skipif(IS_WINDOWS, reason="pty sessions are POSIX-only")

_WRITE_PID = """\
import os, sys
def write(name, pid):
    path = os.path.join(sys.argv[1], name)
    open(path + ".tmp", "w").write(str(pid))
    os.rename(path + ".tmp", path + ".pid")
"""

# The review's layout, all in one POSIX session led by this script: a job P in
# its own group with a child C, and O, double-forked out of P's tree (parent:
# init) but still in P's group. O ignores HUP and logs every SIGCONT it gets.
_ORPHANED_GROUP = (
    _WRITE_PID
    + """\
import signal, time
def o_main():
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    def on_cont(*_):
        with open(os.path.join(sys.argv[1], "cont.log"), "a") as log:
            log.write("cont\\n")
    signal.signal(signal.SIGCONT, on_cont)
    write("o", os.getpid())
    while True:
        time.sleep(0.001)
def p_main():
    os.setpgid(0, 0)
    if os.fork() == 0:
        write("c", os.getpid())
        while True:
            time.sleep(1)
    mid = os.fork()
    if mid == 0:
        if os.fork() == 0:
            o_main()
        os._exit(0)
    os.waitpid(mid, 0)
    write("p", os.getpid())
    while True:
        time.sleep(1)
if os.fork() == 0:
    p_main()
while True:
    time.sleep(1)
"""
)

# A session whose member R keeps forking sleeping children (bounded, so a
# failing kill cannot fork-bomb the box).
_RESPAWNER = (
    _WRITE_PID
    + """\
import time
if os.fork() == 0:
    write("r", os.getpid())
    for _ in range(400):
        if os.fork() == 0:
            while True:
                time.sleep(1)
        time.sleep(0.002)
    while True:
        time.sleep(1)
while True:
    time.sleep(1)
"""
)

# A leader with one sleeping child, the child's pid published.
_LEADER_WITH_CHILD = (
    _WRITE_PID
    + """\
import time
if os.fork() == 0:
    write("child", os.getpid())
    while True:
        time.sleep(1)
while True:
    time.sleep(1)
"""
)


def _wait(predicate: Callable[[], bool], timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _exited(pid: int) -> bool:
    """Reaped, or a zombie awaiting its parent's reap: it can no longer run."""
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


def _launch(tmp_path: Path, body: str, reaper: PtyReaper) -> subprocess.Popen[bytes]:
    """Run `body` as its own session leader (the stand-in shell)."""
    script = tmp_path / "leader.py"
    script.write_text(body, encoding="utf-8")
    leader = subprocess.Popen(  # noqa: S603 — the test's own interpreter and tmp script
        [sys.executable, str(script), str(tmp_path)], start_new_session=True
    )
    reaper.track(psutil.Process(leader.pid))
    return leader


def _pid(tmp_path: Path, name: str, reaper: PtyReaper) -> int:
    path = tmp_path / f"{name}.pid"
    assert _wait(path.exists), f"{name}.pid was never written"
    pid = int(path.read_text(encoding="utf-8"))
    reaper.track(psutil.Process(pid))
    return pid


def _identity(pid: int) -> OwnedProcess:
    return OwnedProcess.capture(psutil.Process(pid))


def _reap(leader: subprocess.Popen[bytes]) -> None:
    """Collect the leader (the test's own child) once it has been killed."""
    if _wait(lambda: _exited(leader.pid)):
        leader.wait(timeout=10)


def test_kill_never_wakes_a_member_whose_group_it_orphans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pty_reaper: PtyReaper
) -> None:
    """O shares job P's group but left the tree. Were P killed first, P's exit
    would orphan the group while O is still stopped, and the kernel would
    SIGHUP+SIGCONT O awake. O dies before P, and a slow liveness probe (a
    loaded box) no longer sits between two SIGKILLs."""
    leader = _launch(tmp_path, _ORPHANED_GROUP, pty_reaper)
    orphan, job, child = (_pid(tmp_path, name, pty_reaper) for name in ("o", "p", "c"))
    assert os.getpgid(orphan) == job and os.getsid(orphan) == leader.pid
    assert psutil.Process(orphan).ppid() != job, "precondition: O left P's tree"
    real_live = session_tree._live

    def slow_live(identity: OwnedProcess) -> bool:
        time.sleep(0.005)
        return real_live(identity)

    monkeypatch.setattr(session_tree, "_live", slow_live)

    result = session_tree.kill_session_tree(_identity(leader.pid), wait_s=3.0)

    assert result.survivors == ()
    assert all(_wait(lambda pid=pid: _exited(pid)) for pid in (orphan, job, child, leader.pid))
    assert not (tmp_path / "cont.log").exists(), "a frozen member was SIGCONTed awake"
    _reap(leader)


def test_kill_takes_what_a_running_member_forks_during_the_kill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pty_reaper: PtyReaper,
    loguru_records: list[dict[str, Any]],
) -> None:
    """A member whose stop never lands keeps forking through the freeze passes
    and the kill. With the shell still frozen its session id still names only
    this session, so a second closure takes every child the member forked
    before it died; running out of freeze passes is logged, never silent."""
    leader = _launch(tmp_path, _RESPAWNER, pty_reaper)
    respawner = _pid(tmp_path, "r", pty_reaper)
    assert _wait(lambda: len(psutil.Process(respawner).children()) >= 5)
    real_freeze, real_live = session_tree._freeze, session_tree._live

    def unfreezable(process: psutil.Process) -> bool:
        return False if process.pid == respawner else real_freeze(process)

    def slow_live(identity: OwnedProcess) -> bool:
        time.sleep(0.001)
        return real_live(identity)

    monkeypatch.setattr(session_tree, "_freeze", unfreezable)
    monkeypatch.setattr(session_tree, "_live", slow_live)
    monkeypatch.setattr(session_tree, "_MAX_FREEZE_PASSES", 2)

    session_tree.kill_session_tree(_identity(leader.pid), wait_s=3.0)

    def left() -> list[int]:
        return [
            process.pid
            for process in psutil.process_iter(["cmdline"])
            if str(tmp_path) in (process.info["cmdline"] or ()) and not _exited(process.pid)
        ]

    assert _wait(lambda: not left(), timeout=3.0), f"forked during the kill, left alive: {left()}"
    assert any(
        "freeze passes" in record["message"] and str(leader.pid) in record["message"]
        for record in loguru_records
    ), "running out of freeze passes must be logged"
    _reap(leader)


def test_kill_that_raises_leaves_no_member_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pty_reaper: PtyReaper
) -> None:
    """Members are SIGSTOPped before the kill decides anything; a kill that
    raises midway must not leave them stopped with nobody to resume them."""
    leader = _launch(tmp_path, _LEADER_WITH_CHILD, pty_reaper)
    child = _pid(tmp_path, "child", pty_reaper)
    real_capture = session_tree._capture_pass
    calls: list[int] = []

    def failing_capture(*args: Any, **kwargs: Any) -> bool:
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("injected fault after the first freeze pass")
        return real_capture(*args, **kwargs)

    monkeypatch.setattr(session_tree, "_capture_pass", failing_capture)

    with pytest.raises(RuntimeError, match="injected fault"):
        session_tree.kill_session_tree(_identity(leader.pid), wait_s=3.0)

    assert _wait(lambda: _exited(child), timeout=5.0), "a frozen member was left stopped"
    assert _wait(lambda: _exited(leader.pid), timeout=5.0), "the frozen leader was left stopped"
    _reap(leader)


# A session leader with a member M (its child, still in its session) and U,
# double-forked out of its tree but still in its session.
_MEMBER_AND_ORPHAN = (
    _WRITE_PID
    + """\
import time
if os.fork() == 0:
    write("m", os.getpid())
    while True:
        time.sleep(1)
mid = os.fork()
if mid == 0:
    if os.fork() == 0:
        write("u", os.getpid())
        while True:
            time.sleep(1)
    os._exit(0)
os.waitpid(mid, 0)
while True:
    time.sleep(1)
"""
)


def _dead_leader(tmp_path: Path, reaper: PtyReaper) -> tuple[OwnedProcess, OwnedProcess, int]:
    """A session whose shell is gone: its identity, a captured member M, and U.

    U never was captured (born after the capture, or missed by it); M was.
    Both are still in the dead shell's POSIX session.
    """
    leader = _launch(tmp_path, _MEMBER_AND_ORPHAN, reaper)
    member, orphan = (_pid(tmp_path, name, reaper) for name in ("m", "u"))
    shell = _identity(leader.pid)
    captured = _identity(member)
    leader.kill()
    leader.wait(timeout=10)
    assert os.getsid(orphan) == shell.pid and os.getsid(member) == shell.pid
    return shell, captured, orphan


def test_a_captured_member_proves_the_dead_shells_session(
    tmp_path: Path, pty_reaper: PtyReaper
) -> None:
    """The shell is dead, so it cannot vouch for its session id. A captured
    member still alive in that session can: the kernel gives no new session an
    id another session still carries, so every process reading that id during
    the pass is in the member's session. The orphan nobody captured dies too."""
    shell, member, orphan = _dead_leader(tmp_path, pty_reaper)

    result = session_tree.kill_session_tree(shell, also=(member,), wait_s=3.0)

    assert result.survivors == ()
    assert _wait(lambda: _exited(member.pid)), "the captured member survived"
    assert _wait(lambda: _exited(orphan)), "the session's orphan survived its session's kill"


def test_nothing_is_taken_by_a_session_id_nobody_proves(
    tmp_path: Path, pty_reaper: PtyReaper, loguru_records: list[dict[str, Any]]
) -> None:
    """With the shell and every captured member gone, nothing proves the id
    still names that session (the pid may have been handed on): a process
    reading it is logged and left alone, never killed."""
    shell, member, orphan = _dead_leader(tmp_path, pty_reaper)
    os.kill(member.pid, signal.SIGKILL)
    assert _wait(lambda: _exited(member.pid))

    result = session_tree.kill_session_tree(shell, also=(member,), wait_s=3.0)

    assert result == session_tree.TreeKill((), ())
    assert not _exited(orphan), "a process was killed on an unproven session id"
    assert any(
        str(orphan) in record["message"] and "prove" in record["message"]
        for record in loguru_records
    ), "an unproven session id must be logged, never silent"


def test_a_recycled_shell_pid_names_no_session(tmp_path: Path, pty_reaper: PtyReaper) -> None:
    """The shell died and a process captured with the session now holds its
    pid, leading a session of its own. It is killed as a captured process, but
    its session is not the shell's: its orphan is left alone."""
    holder = _launch(tmp_path, _MEMBER_AND_ORPHAN, pty_reaper)
    held_member, held_orphan = (_pid(tmp_path, name, pty_reaper) for name in ("m", "u"))
    # The shell's identity, with the pid the kernel handed to `holder`.
    stale = _identity(holder.pid)
    stale = OwnedProcess(
        stale.pid,
        stale.birth - 100.0,
        None if stale.starttime is None else stale.starttime - 10_000,
    )

    session_tree.kill_session_tree(stale, also=(_identity(holder.pid),), wait_s=3.0)

    assert _wait(lambda: _exited(holder.pid)), "the captured holder survived"
    assert _wait(lambda: _exited(held_member)), "the holder's own tree survived"
    assert not _exited(held_orphan), "the holder's own session was taken as the shell's"
    _reap(holder)


def test_a_dead_members_recycled_pid_vouches_for_nothing(
    tmp_path: Path, pty_reaper: PtyReaper
) -> None:
    """The first round kills an orphaned member O, and its pid stays in the
    membership. Were the kernel to hand O's pid to a stranger before the second
    scan, the stranger's child must not be taken for O's: a pid vouches for a
    child only while it still is the captured process. The recycle is simulated
    — the scan and the child's parent read name O's pid once O is gone."""
    leader = _launch(tmp_path, _ORPHANED_GROUP, pty_reaper)
    orphan, _job, _child = (_pid(tmp_path, name, pty_reaper) for name in ("o", "p", "c"))
    gone = psutil.Process(orphan)
    other = tmp_path / "other"
    other.mkdir()
    stranger = _launch(other, _LEADER_WITH_CHILD, pty_reaper)
    victim = _pid(other, "child", pty_reaper)
    real_scan, real_ppid = session_tree._scan, psutil.Process.ppid

    def recycled_scan() -> session_tree._Table:
        table = real_scan()
        if not gone.is_running():
            table.parents[victim] = orphan
        return table

    @functools.wraps(real_ppid)  # keeps psutil's oneshot cache hooks
    def ppid(self: psutil.Process) -> int:
        return orphan if self.pid == victim and not gone.is_running() else real_ppid(self)

    # A scoped patch: undone before teardown reaps with the real psutil.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(session_tree, "_scan", recycled_scan)
        patch.setattr(psutil.Process, "ppid", ppid)
        session_tree.kill_session_tree(_identity(leader.pid), wait_s=3.0)

    assert _wait(lambda: _exited(orphan))
    assert not _exited(victim), "a stranger's child was killed through a dead member's pid"
    assert psutil.Process(victim).status() != psutil.STATUS_STOPPED
    _reap(leader)
    stranger.kill()
    stranger.wait(timeout=10)


def test_a_session_nothing_proves_is_still_looked_at_and_logged(
    tmp_path: Path, pty_reaper: PtyReaper, loguru_records: list[dict[str, Any]]
) -> None:
    """A stop's capture with no captured process left and no fresh proof can
    take nothing more, but its session may still hold a process (the stop
    stalled past the proof). The shared scan still reads it: the capture stays
    busy, and the process is logged once with its pid and command name — never
    signalled."""
    shell, member, orphan = _dead_leader(tmp_path, pty_reaper)
    os.kill(member.pid, signal.SIGKILL)
    assert _wait(lambda: _exited(member.pid))
    capture = session_tree.SessionCapture(shell, [shell, member], None)

    assert session_tree.refresh([capture]) is True, "the session still holds a process"
    assert session_tree.refresh([capture]) is True

    assert orphan not in {identity.pid for identity in capture.members}
    assert not _exited(orphan), "an unproven process was signalled"
    warnings = [
        record["message"]
        for record in loguru_records
        if str(orphan) in record["message"] and "prove" in record["message"]
    ]
    assert len(warnings) == 1, f"logged once, with its command name: {warnings}"
    assert repr(psutil.Process(orphan).name()) in warnings[0]


# The caller alone in a session: its shell leaves, and it asks whether the
# session still holds a process (a stop run from inside the session it closes).
_CALLER_IN_SESSION = """
import os, time, psutil
from base.native_process.ownership import OwnedProcess
from base.sessions.pty import session_tree
shell = OwnedProcess.capture(psutil.Process())
if os.fork() != 0:
    os._exit(0)
while shell.live():
    time.sleep(0.01)
capture = session_tree.SessionCapture(shell, [shell], time.monotonic())
print(session_tree.refresh([capture]), flush=True)
"""


def test_the_caller_does_not_keep_its_own_session_busy() -> None:
    """A stop run from inside a session it closes (`nohup ava stop` in an
    agent's shell) reads its own process there. That is not a process the stop
    could take or wait for, so it does not hold the grace open."""
    result = subprocess.run(  # noqa: S603 — the test's own interpreter
        [sys.executable, "-c", _CALLER_IN_SESSION],
        start_new_session=True,
        capture_output=True,
        text=True,
        timeout=60,
        cwd=Path(__file__).resolve().parents[4],
        env={**os.environ, "AVA_CONFIG_FETCH": "skip"},
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False", "the caller's own process kept its session busy"


def test_a_session_row_gone_since_the_scan_still_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    """A process the scan read in the session but that exited before its pin
    may have forked on its way out: the poll is not quiet."""
    reaped: list[int] = []
    for _ in range(2):
        process = subprocess.Popen([sys.executable, "-c", ""])
        process.wait(timeout=30)
        reaped.append(process.pid)
    shell = OwnedProcess(reaped[0], 1.0, None)
    real_scan = session_tree._scan

    def scan() -> session_tree._Table:
        table = real_scan()
        table.sessions[reaped[1]] = shell.pid
        return table

    monkeypatch.setattr(session_tree, "_scan", scan)
    capture = session_tree.SessionCapture(shell, [shell], None)

    assert session_tree.refresh([capture]) is True, "a row that exited since the read was ignored"


def test_a_logged_command_name_is_quoted_and_capped() -> None:
    """The command name in an unproven-process log line is shown the way the
    closure notice shows it: quoted, escaped and capped."""
    name = "evil\nname " + "x" * 200
    process = cast("psutil.Process", SimpleNamespace(name=lambda: name))

    shown = session_tree._command(process)

    assert shown == shown_name(name)
    assert "\n" not in shown and "x" * 100 not in shown
