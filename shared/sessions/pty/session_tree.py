"""The processes a PTY session owns, captured by identity and killed whole.

A session's membership is its shell, every descendant of the shell, and every
process in the shell's POSIX session (``getsid(pid) == shell pid``) together
with that process's descendants. Every path that ends a session goes through
`kill_session_tree`: the host's ``kill`` op, the CLI's record-based kill of a
wedged host, the lazy sweep of a crashed host's shell, and the orphan-host
reaper (`kill_host_tree`).

Why the POSIX session is the membership test, not process groups or the tty:

- Job control gives every job its own process group, and a double-forked job
  member (``(cmd &)``, ``nohup cmd &`` from a subshell) keeps the group of a
  job whose leader has already exited. A group scan finds such a process only
  while a captured member still shares its group.
- The controlling terminal is an attribute of the session: a process whose
  controlling tty is the session's pty is necessarily in the shell's session,
  and a member that dropped the tty (TIOCNOTTY) or outlived its hangup keeps
  the session id. The session id covers both and costs one getsid(2) per pid.
  The kernel never hands out a pid that still names a live session, so the id
  cannot be recycled onto a stranger while a member remains
  (`shared.proc.hosting_exec_domain` answers the same question the same way).
- A process that calls setsid(2) AND has left the shell's tree has left the
  session by the kernel's own definition. That is how Ava launches a sovereign
  process from inside a shell (`shared._reparent`: setsid, fork, reparent to
  init — a new PTY host, the services `ava start` brings up), and no
  birth-identified, name-free fact separates it from any other daemon, so it
  survives the kill. A setsid'd process still inside the tree is covered by
  the descendant walk.

Kill sequence: pin each member (a psutil object, which refuses a recycled pid,
plus its `OwnedProcess` birth identity) before any signal and SIGSTOP it,
parents first. Once every frozen member is observed stopped, scan again, until
a pass finds nobody new: a stopped process cannot fork, so the set is closed.
Then SIGKILL children before parents with the shell last (a job never sees its
group orphaned while it still has stopped members, so the kernel's
SIGHUP+SIGCONT cannot wake one), wait, and report every captured member still
alive. Nothing is ever selected by name or argv.
"""

from __future__ import annotations

import contextlib
import os
import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass

import psutil

from shared.proc_tree import OwnedProcess

# How long one freeze pass waits for its SIGSTOPs to land before rescanning.
# A member in uninterruptible sleep stops late; the next pass still sees it.
_STOP_SETTLE_S = 1.0

# Upper bound on freeze passes. A pass only adds processes forked before the
# previous pass's stops landed, so a real session closes in two or three.
_MAX_FREEZE_PASSES = 32

_POLL_S = 0.01

# A member in one of these states cannot fork any more.
_SETTLED = frozenset(
    {
        psutil.STATUS_STOPPED,
        psutil.STATUS_TRACING_STOP,
        psutil.STATUS_ZOMBIE,
        psutil.STATUS_DEAD,
    }
)


@dataclass(frozen=True)
class _Member:
    identity: OwnedProcess
    process: psutil.Process
    frozen: bool


@dataclass(frozen=True)
class TreeKill:
    """What a tree kill did.

    `killed` names the live members it SIGKILLed (empty: everything had
    already exited); `survivors` the captured members still alive after the
    wait (empty: the whole membership is gone).
    """

    killed: tuple[OwnedProcess, ...]
    survivors: tuple[OwnedProcess, ...]


def _scan() -> tuple[dict[int, int], dict[int, int]]:
    """One pass over the process table: parent pid and session id per pid."""
    parents: dict[int, int] = {}
    sessions: dict[int, int] = {}
    for proc in psutil.process_iter(["ppid"]):
        ppid = proc.info["ppid"]
        if isinstance(ppid, int):
            parents[proc.pid] = ppid
        with contextlib.suppress(OSError):
            sessions[proc.pid] = os.getsid(proc.pid)
    return parents, sessions


def _children_of(parents: dict[int, int]) -> dict[int, list[int]]:
    children: dict[int, list[int]] = {}
    for pid, ppid in parents.items():
        children.setdefault(ppid, []).append(pid)
    return children


def _with_descendants(roots: set[int], children: dict[int, list[int]]) -> set[int]:
    members = set(roots)
    stack = list(roots)
    while stack:
        fresh = [child for child in children.get(stack.pop(), ()) if child not in members]
        members.update(fresh)
        stack.extend(fresh)
    return members


def _top_down(roots: set[int], parents: dict[int, int]) -> list[int]:
    """`roots` and their descendants in one snapshot, every parent before its children."""
    children = _children_of(parents)
    members = _with_descendants(roots, children)
    queue = deque(sorted(pid for pid in members if parents.get(pid) not in members))
    order: list[int] = []
    while queue:
        pid = queue.popleft()
        order.append(pid)
        queue.extend(child for child in children.get(pid, ()) if child in members)
    return order


def _signallable(pid: int) -> bool:
    """Never the kernel, init, or the caller (a record-based kill may run
    inside the session it kills)."""
    return pid > 1 and pid != os.getpid()


def _pin_identity(identity: OwnedProcess) -> psutil.Process | None:
    """A psutil handle on `identity`, or None unless it provably still is that process.

    Verified after construction, so the handle pins the recorded birth. An
    identity that cannot be verified is never signalled (and `_live` never
    certifies it gone either).
    """
    try:
        process = psutil.Process(identity.pid)
        return process if identity.live() else None
    except (psutil.NoSuchProcess, RuntimeError):
        return None


def _pin_new(pid: int, members: dict[int, _Member], sid: int | None) -> _Member | None:
    """Pin `pid` when the pinned process itself belongs to the session.

    Membership is re-read on the pinned handle — its parent is a captured
    member, or its session is the shell's — and `is_running` afterwards proves
    those reads described that handle, so a pid recycled since the scan is
    never captured. A zombie cannot execute and is skipped.
    """
    try:
        process = psutil.Process(pid)
        belongs = process.ppid() in members or (sid is not None and os.getsid(pid) == sid)
        if not belongs or process.status() == psutil.STATUS_ZOMBIE:
            return None
        identity = OwnedProcess.capture(process)
        if not process.is_running():
            return None
    except (psutil.Error, ProcessLookupError):
        return None
    return _Member(identity, process, frozen=False)


def _freeze(process: psutil.Process) -> bool:
    """SIGSTOP one pinned member; False when it could not be stopped."""
    try:
        process.suspend()
    except psutil.Error:
        # Exited, or not ours to signal (a setuid member): still captured, and
        # reported as a survivor if it outlives the kill.
        return False
    return True


def _pin_roots(
    leader: OwnedProcess, also: Iterable[OwnedProcess], *, freeze: bool
) -> dict[int, _Member]:
    """Pin the leader and the extra roots that are still the captured processes."""
    members: dict[int, _Member] = {}
    for identity in (leader, *also):
        if identity.pid in members or not _signallable(identity.pid):
            continue
        process = _pin_identity(identity)
        if process is not None:
            members[identity.pid] = _Member(identity, process, freeze and _freeze(process))
    return members


def _capture_pass(members: dict[int, _Member], sid: int | None, *, freeze: bool) -> bool:
    """Add every member the current process table shows; True when one was new."""
    parents, sessions = _scan()
    roots = set(members)
    if sid is not None:
        roots |= {pid for pid, session in sessions.items() if session == sid}
    added = False
    for pid in _top_down(roots, parents):
        if pid in members or not _signallable(pid):
            continue
        member = _pin_new(pid, members, sid)
        if member is None:
            continue
        members[pid] = _Member(member.identity, member.process, freeze and _freeze(member.process))
        added = True
    return added


def _settled(member: _Member) -> bool:
    if not member.frozen:
        return True
    try:
        return member.process.status() in _SETTLED
    except psutil.NoSuchProcess:
        return True
    except psutil.AccessDenied:
        return True  # unreadable: nothing further to wait for


def _await_stopped(members: Iterable[_Member]) -> None:
    pending = [member for member in members if member.frozen]
    deadline = time.monotonic() + _STOP_SETTLE_S
    while pending and time.monotonic() < deadline:
        pending = [member for member in pending if not _settled(member)]
        if pending:
            time.sleep(_POLL_S)


def _live(identity: OwnedProcess) -> bool:
    try:
        return identity.live()
    except RuntimeError:
        return True  # an identity that cannot be verified is never certified gone


def _sigkill(process: psutil.Process) -> bool:
    """SIGKILL one pinned member; False when it had already exited."""
    try:
        process.kill()
    except psutil.NoSuchProcess:
        return False  # exited meanwhile; psutil also refuses a recycled pid
    except psutil.AccessDenied:
        return True  # not ours to kill: `_await_exit` reports it as a survivor
    return True


def _kill(members: Iterable[_Member]) -> list[OwnedProcess]:
    """SIGKILL each still-live member in order; return those signalled."""
    return [
        member.identity for member in members if _live(member.identity) and _sigkill(member.process)
    ]


def _await_exit(members: Iterable[_Member], wait_s: float) -> list[OwnedProcess]:
    """Captured members still alive once they all exited or `wait_s` passed."""
    deadline = time.monotonic() + wait_s
    live = [member.identity for member in members]
    while True:
        live = [identity for identity in live if _live(identity)]
        if not live or time.monotonic() >= deadline:
            return live
        time.sleep(_POLL_S)


def session_members(leader: OwnedProcess) -> list[OwnedProcess]:
    """The session's current membership, leader first; nothing is signalled.

    Empty when `leader` is no longer the live shell. A live member beyond the
    leader is running work — the kill op's `interrupted` verdict.
    """
    members = _pin_roots(leader, (), freeze=False)
    if leader.pid not in members:
        return []
    _capture_pass(members, leader.pid, freeze=False)
    return [member.identity for member in members.values()]


def terminate(members: Iterable[OwnedProcess]) -> None:
    """SIGTERM each member that is still the captured process (graceful kill)."""
    for identity in members:
        process = _pin_identity(identity)
        if process is not None:
            with contextlib.suppress(psutil.Error):
                process.terminate()


def kill_session_tree(
    leader: OwnedProcess, *, also: Iterable[OwnedProcess] = (), wait_s: float
) -> TreeKill:
    """Freeze, then SIGKILL, the whole membership of `leader`'s session.

    `leader` is the session's shell. `also` adds roots captured earlier (a
    graceful kill's pre-TERM snapshot), so their trees are still taken when
    the shell itself already died. The session-id scan runs only while the
    leader is the verified live shell. Children die before parents and the
    leader dies last, after the rest were waited for; `wait_s` bounds each of
    the two waits.
    """
    members = _pin_roots(leader, also, freeze=True)
    sid = leader.pid if leader.pid in members else None
    for _ in range(_MAX_FREEZE_PASSES):
        _await_stopped(members.values())
        if not _capture_pass(members, sid, freeze=True):
            break
    order = list(members.values())
    body = [member for member in reversed(order) if member.identity.pid != leader.pid]
    head = [member for member in order if member.identity.pid == leader.pid]
    killed = _kill(body)
    survivors = _await_exit(body, wait_s)
    killed += _kill(head)
    survivors += _await_exit(head, wait_s)
    return TreeKill(tuple(killed), tuple(survivors))


def kill_host_tree(host: psutil.Process, *, wait_s: float) -> TreeKill:
    """Kill a session host after every session its shells lead.

    The host is frozen first so it cannot fork a shell behind the sweep; each
    direct child (the host's shell, a session leader) is killed with its whole
    session, then the host itself. `host` is a pinned handle, so a recycled
    pid is never signalled.
    """
    try:
        if not _signallable(host.pid) or not host.is_running():
            return TreeKill((), ())
        head = [_Member(OwnedProcess.capture(host), host, frozen=False)]
    except psutil.NoSuchProcess:
        return TreeKill((), ())
    _freeze(host)
    killed: list[OwnedProcess] = []
    survivors: list[OwnedProcess] = []
    for shell in _child_identities(host):
        result = kill_session_tree(shell, wait_s=wait_s)
        killed += result.killed
        survivors += result.survivors
    killed += _kill(head)
    survivors += _await_exit(head, wait_s)
    return TreeKill(tuple(killed), tuple(survivors))


def _child_identities(process: psutil.Process) -> list[OwnedProcess]:
    identities: list[OwnedProcess] = []
    with contextlib.suppress(psutil.NoSuchProcess):
        for child in process.children():
            with contextlib.suppress(psutil.NoSuchProcess):
                identities.append(OwnedProcess.capture(child))
    return identities
