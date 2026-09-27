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
a pass finds nobody new: a stopped process cannot fork, so the set is closed (a
pass cap that runs out first is logged). Then SIGKILL every member but the
shell in one tight loop, liveness read before the first signal, children
before parents and the shell's own tree last. The order matters because the
kernel SIGHUP+SIGCONTs the stopped members of a process group the moment an
exit orphans it: a job's leader is what ties the job's group to the session,
and a member that double-forked out of the tree may still share that group, so
it dies before the leader does. The shell is still frozen and alive after that
batch, so the session id still names only this session: one more closure
catches anything that ran meanwhile and kills it too. Then the shell dies, and
every captured member still alive after the wait is reported. A kill that
raises midway SIGKILLs every member it froze on the way out, so none is left
stopped with nobody to resume it. Nothing is ever selected by name or argv.
"""

from __future__ import annotations

import contextlib
import os
import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field

import psutil

from shared.log import logger
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
    wait (empty: the whole membership is gone); `denied` the survivors the
    caller has no permission to signal at all (another user's process, such as
    a root `sudo` on the pty), which were left running, never frozen.
    """

    killed: tuple[OwnedProcess, ...]
    survivors: tuple[OwnedProcess, ...]
    denied: tuple[OwnedProcess, ...] = ()

    @property
    def stuck(self) -> tuple[OwnedProcess, ...]:
        """The survivors the caller could signal: the kill did not take them."""
        return tuple(identity for identity in self.survivors if identity not in self.denied)


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


def _top_down(roots: set[int], parents: dict[int, int], first: int | None) -> list[int]:
    """`roots` and their descendants in one snapshot, every parent before its children.

    Each top-level process brings its whole subtree, `first` (the live shell)
    before every other: reversed for the kill, a process that left the shell's
    tree dies before the job leaders inside it.
    """
    children = _children_of(parents)
    members = _with_descendants(roots, children)
    tops = sorted(
        (pid for pid in members if parents.get(pid) not in members),
        key=lambda pid: (pid != first, pid),
    )
    order: list[int] = []
    for top in tops:
        queue = deque([top])
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
    leader: OwnedProcess,
    also: Iterable[OwnedProcess],
    members: dict[int, _Member],
    *,
    freeze: bool,
) -> None:
    """Pin the leader and the extra roots that are still the captured processes."""
    for identity in (leader, *also):
        if identity.pid in members or not _signallable(identity.pid):
            continue
        process = _pin_identity(identity)
        if process is not None:
            members[identity.pid] = _Member(identity, process, freeze and _freeze(process))


def _capture_pass(members: dict[int, _Member], sid: int | None, *, freeze: bool) -> bool:
    """Add every member the current process table shows; True when one was new.

    Members already captured move to their place in this snapshot, so the
    dict stays parents-first whatever order the roots were pinned in.
    """
    parents, sessions = _scan()
    roots = set(members)
    if sid is not None:
        roots |= {pid for pid, session in sessions.items() if session == sid}
    added = False
    for pid in _top_down(roots, parents, sid):
        if pid in members:
            members[pid] = members.pop(pid)
            continue
        if not _signallable(pid):
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


def _close(members: dict[int, _Member], sid: int | None) -> bool:
    """Freeze passes until one adds nobody; False when the pass cap ran out first."""
    for _ in range(_MAX_FREEZE_PASSES):
        _await_stopped(members.values())
        if not _capture_pass(members, sid, freeze=True):
            return True
    return False


def _live(identity: OwnedProcess) -> bool:
    try:
        return identity.live()
    except (RuntimeError, psutil.AccessDenied):
        return True  # an identity that cannot be verified is never certified gone


def _kill(batch: list[_Member], done: set[int]) -> tuple[list[OwnedProcess], list[OwnedProcess]]:
    """SIGKILL every still-live member of `batch` in one tight loop: (killed, denied).

    Liveness is read for the whole batch before the first signal, so no probe
    sits between two SIGKILLs. Each member joins `done` once its signal went
    out (or it had exited); `denied` names those the caller may not signal.
    """
    live = [member for member in batch if _live(member.identity)]
    killed: list[OwnedProcess] = []
    denied: list[OwnedProcess] = []
    for member in live:
        # NoSuchProcess: exited meanwhile; psutil also refuses a recycled pid.
        with contextlib.suppress(psutil.NoSuchProcess):
            try:
                member.process.kill()
                killed.append(member.identity)
            except psutil.AccessDenied:
                denied.append(member.identity)
        done.add(member.identity.pid)
    done.update(member.identity.pid for member in batch)
    return killed, denied


def _await_exit(members: Iterable[_Member], wait_s: float) -> list[OwnedProcess]:
    """Captured members still alive once they all exited or `wait_s` passed."""
    deadline = time.monotonic() + wait_s
    live = [member.identity for member in members]
    while True:
        live = [identity for identity in live if _live(identity)]
        if not live or time.monotonic() >= deadline:
            return live
        time.sleep(_POLL_S)


@dataclass
class _Outcome:
    """What a kill signalled and what outlived it, gathered batch by batch."""

    killed: list[OwnedProcess] = field(default_factory=list[OwnedProcess])
    denied: list[OwnedProcess] = field(default_factory=list[OwnedProcess])
    survivors: list[OwnedProcess] = field(default_factory=list[OwnedProcess])

    def kill(self, batch: list[_Member], done: set[int], wait_s: float) -> None:
        """SIGKILL `batch` in one tight loop, then wait for it to exit."""
        killed, denied = _kill(batch, done)
        self.killed += killed
        self.denied += denied
        self.survivors += _await_exit(batch, wait_s)

    def add(self, result: TreeKill) -> None:
        self.killed += result.killed
        self.denied += result.denied
        self.survivors += result.survivors

    def result(self) -> TreeKill:
        denied = tuple(identity for identity in self.denied if identity in self.survivors)
        return TreeKill(tuple(self.killed), tuple(self.survivors), denied)


def _kill_stranded(members: Iterable[_Member], done: set[int]) -> None:
    """SIGKILL every frozen member a raising kill never reached.

    A no-op when the kill completed: every member went through `_kill`. A
    member that cannot be killed is resumed instead of left stopped.
    """
    for member in members:
        if not member.frozen or member.identity.pid in done:
            continue
        try:
            member.process.kill()
        except psutil.NoSuchProcess:
            continue
        except psutil.Error:
            with contextlib.suppress(psutil.Error):
                member.process.resume()


def session_members(leader: OwnedProcess) -> list[OwnedProcess]:
    """The session's current membership, leader first; nothing is signalled.

    Empty when `leader` is no longer the live shell. A live member beyond the
    leader is running work — the kill op's `interrupted` verdict.
    """
    members: dict[int, _Member] = {}
    _pin_roots(leader, (), members, freeze=False)
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
    leader is the verified live shell. The leader dies last, after the rest
    were waited for; `wait_s` bounds each wait. When anything raises midway,
    every member frozen and not yet killed is SIGKILLed on the way out.
    """
    members: dict[int, _Member] = {}
    done: set[int] = set()
    try:
        return _kill_frozen(leader, also, members, done, wait_s)
    finally:
        _kill_stranded(members.values(), done)


def _kill_frozen(
    leader: OwnedProcess,
    also: Iterable[OwnedProcess],
    members: dict[int, _Member],
    done: set[int],
    wait_s: float,
) -> TreeKill:
    _pin_roots(leader, also, members, freeze=True)
    sid = leader.pid if leader.pid in members else None
    outcome = _Outcome()
    # Two rounds: the second closure runs while the leader is still frozen (its
    # session id still names only this session) and takes whatever ran or
    # forked while the first batch was dying.
    for _ in range(2):
        if not _close(members, sid):
            logger.warning(
                "pty session {leader}: still finding new members after {passes} freeze "
                "passes; a process forked during the last pass may escape the kill",
                leader=leader.pid,
                passes=_MAX_FREEZE_PASSES,
            )
        body = [
            member
            for member in reversed(members.values())
            if member.identity.pid != leader.pid and member.identity.pid not in done
        ]
        outcome.kill(body, done, wait_s)
    outcome.kill([members[leader.pid]] if leader.pid in members else [], done, wait_s)
    return outcome.result()


def kill_host_tree(host: psutil.Process, *, wait_s: float) -> TreeKill:
    """Kill a session host after every session its shells lead.

    The host is frozen first so it cannot fork a shell behind the sweep; each
    direct child (the host's shell, a session leader) is killed with its whole
    session, then the host itself — also when a session kill raised, so the
    host is never left stopped. `host` is a pinned handle, so a recycled pid
    is never signalled.
    """
    try:
        if not _signallable(host.pid) or not host.is_running():
            return TreeKill((), ())
        head = [_Member(OwnedProcess.capture(host), host, frozen=False)]
    except psutil.NoSuchProcess:
        return TreeKill((), ())
    _freeze(host)
    outcome = _Outcome()
    try:
        for shell in _child_identities(host):
            outcome.add(kill_session_tree(shell, wait_s=wait_s))
    finally:
        outcome.kill(head, set(), wait_s)
    return outcome.result()


def _child_identities(process: psutil.Process) -> list[OwnedProcess]:
    identities: list[OwnedProcess] = []
    with contextlib.suppress(psutil.NoSuchProcess):
        for child in process.children():
            with contextlib.suppress(psutil.NoSuchProcess):
                identities.append(OwnedProcess.capture(child))
    return identities
