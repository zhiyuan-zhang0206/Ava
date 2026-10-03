"""The processes a PTY session owns, captured by identity and killed whole.

A session's membership is its shell, every descendant of the shell, and every
process in the shell's POSIX session (``getsid(pid) == shell pid``) together
with that process's descendants. Every path that ends a session goes through
`kill_session_tree`: the service's ``kill`` op and the SIGKILL leg of the
persistent-terminal closure (`closure`: a normal `ava stop`, the service's own
stop, the sweep of a crashed service's leftovers).

Why the POSIX session is the membership test, not process groups or the tty:

- Job control gives every job its own process group, and a double-forked job
  member (``(cmd &)``, ``nohup cmd &`` from a subshell) keeps the group of a
  job whose leader has already exited. A group scan finds such a process only
  while a captured member still shares its group.
- The controlling terminal is an attribute of the session: a process whose
  controlling tty is the session's pty is necessarily in the shell's session,
  and a member that dropped the tty (TIOCNOTTY) or outlived its hangup keeps
  the session id. The session id covers both and costs one getsid(2) per pid.
- A process that calls setsid(2) AND has left the shell's tree has left the
  session by the kernel's own definition. That is how Ava launches a sovereign
  process from inside a shell (`base._reparent`: setsid, fork, reparent to
  init — the services `ava start` brings up), and no
  birth-identified, name-free fact separates it from any other daemon, so it
  survives the kill. A setsid'd process still inside the tree is covered by
  the descendant walk.

A pass takes a process by its session id only while the id is proven to still
name the shell's session: a captured member still in it after the pass's
reads, or a proof under `_PROOF_FRESH_S` old (`_proven`; the kernel argument
is in session-kill.ava.okf.md). Otherwise the process is logged, left alone.

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
it dies before the leader does. The frozen shell (or a member the batch could
not end) still proves the session id after that batch: one more closure
catches anything that ran meanwhile and kills it too. Then the shell dies, and
every captured member still alive after the wait is reported. A kill that
raises midway SIGKILLs every member it froze on the way out, so none is left
stopped with nobody to resume it. Nothing is ever selected by name or argv.
"""

from __future__ import annotations

import contextlib
import os
import signal
import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import NamedTuple

import psutil

from base.log import logger
from base.native_process import pid_starttime_ticks
from base.native_process.ownership import OwnedProcess, shown_name

# How long one freeze pass waits for its SIGSTOPs to land before rescanning.
# A member in uninterruptible sleep stops late; the next pass still sees it.
_STOP_SETTLE_S = 1.0

# Upper bound on freeze passes. A pass only adds processes forked before the
# previous pass's stops landed, so a real session closes in two or three.
_MAX_FREEZE_PASSES = 32

_POLL_S = 0.01

# How long a session-id proof stands once no captured member is left to renew
# it: the original session could only have been replaced by a new one under the
# same id if it ended and the kernel handed the pid out again inside this
# window. Pid reuse does not land inside a couple of seconds; this is half that
# (decisions/2026-09-28-session-id-proven-by-a-live-member.md).
_PROOF_FRESH_S = 1.0

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
class _Pin:
    """A process a capture pass pinned, before the pass decides to keep it.

    `by_session`: it belongs only through its session id (itself, or through a
    parent that does), so it is kept only when the pass proves the id.
    """

    member: _Member
    by_session: bool


@dataclass
class _Proof:
    """When the session id was last proven to name the shell's session (monotonic).

    `reported` holds the pids already logged as unproven, so a process the
    proof cannot cover is logged once per kill or per stop, not every pass.
    """

    at: float | None = None
    reported: set[int] = field(default_factory=set[int])

    def fresh(self) -> bool:
        return self.at is not None and time.monotonic() - self.at <= _PROOF_FRESH_S


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


class _Table(NamedTuple):
    """One pass over the process table, and when it began (monotonic)."""

    parents: dict[int, int]
    sessions: dict[int, int]
    started: float


def _scan() -> _Table:
    """Parent pid per pid, then session id per pid.

    The session ids come last, from a bare getsid(2) per pid: ~0.2 ms for
    ~800 processes against ~16 ms for the psutil pass. Swept after that pass,
    a fork-and-exit hop of a few ms is read while it exists and pinned (in a
    kill, frozen) about a millisecond later; every pin re-reads its parent,
    so the older parent map costs nothing. Highest pid first reads the newest
    first only until pids wrap; after that they come last, still inside the
    sweep's fraction of a millisecond.
    """
    started = time.monotonic()
    parents: dict[int, int] = {}
    for proc in psutil.process_iter(["ppid"]):
        ppid = proc.info["ppid"]
        if isinstance(ppid, int):
            parents[proc.pid] = ppid
    sessions: dict[int, int] = {}
    for pid in sorted(psutil.pids(), reverse=True):
        with contextlib.suppress(OSError):
            sessions[pid] = os.getsid(pid)
    return _Table(parents, sessions, started)


def _occupied(sessions: dict[int, int], sid: int) -> bool:
    """Whether the scan read a process in session `sid` that was not a zombie.

    One gone since the read still counts: it existed, and may have forked on
    its way out. The caller itself does not count (`_signallable`): a stop
    run from inside a session it closes would otherwise hold the grace open
    for its own process. A process the caller may not signal (a root `sudo`)
    still counts: it is the session's work, which the grace waits for and
    the kill then reports as a survivor, exactly as for a captured one.
    """
    for pid, session in sessions.items():
        if session != sid or not _signallable(pid):
            continue
        try:
            if psutil.Process(pid).status() != psutil.STATUS_ZOMBIE:
                return True
        except psutil.Error:
            return True
    return False


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


def _parent_link(ppid: int, members: dict[int, _Member], fresh: dict[int, _Pin]) -> bool | None:
    """How the process at `ppid` vouches for a child it parents.

    None when it is no captured process — a member whose pid the kernel has
    since handed on vouches for nothing. Otherwise whether the parent itself
    rests on a session read (a pin this pass has not proven yet).
    """
    pin = fresh.get(ppid)
    if pin is not None:
        return pin.by_session if pin.member.process.is_running() else None
    member = members.get(ppid)
    if member is not None and member.process.is_running():
        return False
    return None


def _pin_new(
    pid: int, members: dict[int, _Member], fresh: dict[int, _Pin], sid: int
) -> _Pin | None:
    """Pin `pid` when the pinned process itself belongs to the session.

    Membership is re-read on the pinned handle — its parent is a captured
    process, checked after the parent pid was read, or its session is the
    shell's — and `is_running` afterwards proves those reads described that
    handle, so a pid recycled since the scan is never captured. A zombie
    cannot execute and is skipped.
    """
    try:
        process = psutil.Process(pid)
        by_session = _parent_link(process.ppid(), members, fresh)
        if by_session is None:
            if os.getsid(pid) != sid:
                return None
            by_session = True
        if process.status() == psutil.STATUS_ZOMBIE:
            return None
        identity = OwnedProcess.capture(process)
        if not process.is_running():
            return None
    except (psutil.Error, ProcessLookupError):
        return None
    return _Pin(_Member(identity, process, frozen=False), by_session)


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


def _shell_pid_unclaimed(leader: OwnedProcess) -> bool:
    """Whether the shell's pid is free or still the shell's own (a zombie included).

    Any other process there means the kernel released the pid, which it does
    only once no session carries it as its id: the shell's session is over,
    and the id may now name the new holder's own session.
    """
    try:
        process = psutil.Process(leader.pid)
        if leader.starttime is not None:
            return pid_starttime_ticks(leader.pid) in (leader.starttime, None)
        return leader.birth_matches(process)
    except psutil.NoSuchProcess:
        return True
    except psutil.Error:
        return False


def _witnessed(members: dict[int, _Member], sid: int) -> bool:
    """Whether a captured member, read now, still is the captured process in session `sid`.

    The session id is read first and the identity verified after it, so the
    read described that member. A member leaves its session only by setsid,
    which renames the session to its own pid.
    """
    for member in members.values():
        with contextlib.suppress(OSError, RuntimeError, psutil.Error):
            if os.getsid(member.identity.pid) == sid and member.identity.live():
                return True
    return False


def _proven(members: dict[int, _Member], leader: OwnedProcess, proof: _Proof) -> bool:
    """Whether the session id still names the shell's session, read after a pass's reads.

    A live witness renews the proof; without one, a fresh proof still stands.
    """
    if not _shell_pid_unclaimed(leader):
        return False
    if _witnessed(members, leader.pid):
        proof.at = time.monotonic()
        return True
    return proof.fresh()


def _command(process: psutil.Process) -> str:
    try:
        return shown_name(process.name())
    except psutil.Error:
        return "<unreadable>"


def _unproven(fresh: dict[int, _Pin], leader: OwnedProcess, proof: _Proof) -> dict[int, _Pin]:
    """The pins that do not rest on the session id; the others are logged, never taken."""
    dropped = {pid: pin for pid, pin in fresh.items() if pin.by_session}
    new = sorted(set(dropped) - proof.reported)
    if new:
        proof.reported.update(new)
        logger.warning(
            "pty session {leader}: {processes} carry its session id, but nothing proves the "
            "id still names that session; left running",
            leader=leader.pid,
            processes=", ".join(f"{pid} {_command(dropped[pid].member.process)}" for pid in new),
        )
    return {pid: pin for pid, pin in fresh.items() if not pin.by_session}


def _keep(
    fresh: dict[int, _Pin],
    members: dict[int, _Member],
    leader: OwnedProcess,
    proof: _Proof,
    table: _Table,
) -> dict[int, _Pin]:
    """The pins a pass keeps: all of them when the session id is proven.

    A proven pass that read any process in the session shows the session
    alive at that read, after the scan began, which renews the proof: a chain
    of short-lived processes keeps it current as long as passes keep reading
    one of them.
    """
    if not _proven(members, leader, proof):
        return _unproven(fresh, leader, proof)
    if proof.at is not None and leader.pid in table.sessions.values():
        proof.at = max(proof.at, table.started)
    return fresh


def _capture_pass(
    members: dict[int, _Member],
    leader: OwnedProcess,
    proof: _Proof,
    *,
    freeze: bool,
    table: _Table | None = None,
) -> bool:
    """Add every member the process table shows; True when one was new.

    Descendants of live members join outright. A process that belongs only
    through its session id joins when, after every read of the pass, the id is
    still proven (`_proven`). Nothing is signalled before that. Members already
    captured move to their place in this snapshot, so the dict stays
    parents-first whatever order the roots were pinned in.
    """
    sid = leader.pid
    table = table if table is not None else _scan()
    live = {pid for pid, member in members.items() if member.process.is_running()}
    rows = {pid for pid, session in table.sessions.items() if session == sid}
    order = _top_down(live | rows, table.parents, sid)
    fresh = _keep(_pin_candidates(order, live, members, sid), members, leader, proof, table)
    _place(order, live, fresh, members, freeze=freeze)
    return bool(fresh)


def _pin_candidates(
    order: list[int], live: set[int], members: dict[int, _Member], sid: int
) -> dict[int, _Pin]:
    """Pin, without signalling anything, each process in `order` not yet a live member."""
    fresh: dict[int, _Pin] = {}
    for pid in order:
        if pid not in live and _signallable(pid):
            pin = _pin_new(pid, members, fresh, sid)
            if pin is not None:
                fresh[pid] = pin
    return fresh


def _place(
    order: list[int],
    live: set[int],
    fresh: dict[int, _Pin],
    members: dict[int, _Member],
    *,
    freeze: bool,
) -> None:
    """Move members to their place in this snapshot and add the kept pins, parents first."""
    for pid in order:
        if pid in live:
            members[pid] = members.pop(pid)
        elif pid in fresh:
            member = fresh[pid].member
            members.pop(pid, None)  # a dead member whose pid the kernel handed on
            members[pid] = replace(member, frozen=freeze and _freeze(member.process))


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


def _close(members: dict[int, _Member], leader: OwnedProcess, proof: _Proof) -> bool:
    """Freeze passes until one adds nobody; False when the pass cap ran out first."""
    for _ in range(_MAX_FREEZE_PASSES):
        _await_stopped(members.values())
        if not _capture_pass(members, leader, proof, freeze=True):
            return True
    return False


def _live(identity: OwnedProcess) -> bool:
    try:
        return identity.live()
    except (RuntimeError, psutil.AccessDenied):
        return True  # an identity that cannot be verified is never certified gone


def _kill(
    batch: list[_Member], done: set[OwnedProcess]
) -> tuple[list[OwnedProcess], list[OwnedProcess]]:
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
        done.add(member.identity)
    done.update(member.identity for member in batch)
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

    def kill(self, batch: list[_Member], done: set[OwnedProcess], wait_s: float) -> None:
        """SIGKILL `batch` in one tight loop, then wait for what it signalled to exit.

        A member the caller may not signal got no signal, so no wait can
        change its fate: its liveness is read once instead of costing the
        whole `wait_s` (the TTL reaper's dispatch budget covers the kill op).
        """
        killed, denied = _kill(batch, done)
        self.killed += killed
        self.denied += denied
        refused = set(denied)
        signalled = [member for member in batch if member.identity not in refused]
        self.survivors += _await_exit(signalled, wait_s)
        self.survivors += [identity for identity in denied if _live(identity)]

    def add(self, result: TreeKill) -> None:
        self.killed += result.killed
        self.denied += result.denied
        self.survivors += result.survivors

    def result(self) -> TreeKill:
        denied = tuple(identity for identity in self.denied if identity in self.survivors)
        return TreeKill(tuple(self.killed), tuple(self.survivors), denied)


def _kill_stranded(members: Iterable[_Member], done: set[OwnedProcess]) -> None:
    """SIGKILL every frozen member a raising kill never reached.

    A no-op when the kill completed: every member went through `_kill`. A
    member that cannot be killed is resumed instead of left stopped.
    """
    for member in members:
        if not member.frozen or member.identity in done:
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
    return capture_session(leader).members


@dataclass
class SessionCapture:
    """A session's captured membership, kept current while its members exit.

    `members` holds every process ever captured, leader first, each
    re-verified before it is signalled. `proven_at` is when the session id was
    last proven to name this session (monotonic): within `_PROOF_FRESH_S` of
    it a pass may still take a process by that id after the last captured
    member is gone — a job that forked a helper on TERM and exited leaves the
    helper as the session's only process. `reported` holds the pids already
    logged as unproven.
    """

    leader: OwnedProcess
    members: list[OwnedProcess]
    proven_at: float | None
    reported: set[int] = field(default_factory=set[int])

    @property
    def active(self) -> bool:
        """Whether a kill may still find a process: a captured one lives, or the proof is fresh."""
        return any(_live(identity) for identity in self.members) or _Proof(self.proven_at).fresh()


def _absorb(capture: SessionCapture, members: dict[int, _Member], table: _Table) -> bool:
    """Fold one scan into `capture`; True while its session still holds a process.

    That is a captured process still alive, or any process the scan read in
    the session, pinned or not, even one gone since: a quiet poll must not
    come from a scan that raced a process out of the session.
    """
    proof = _Proof(capture.proven_at, capture.reported)
    _capture_pass(members, capture.leader, proof, freeze=False, table=table)
    capture.proven_at = proof.at
    known = set(capture.members)
    capture.members += [
        member.identity for member in members.values() if member.identity not in known
    ]
    if any(_live(member.identity) for member in members.values()):
        return True
    return _occupied(table.sessions, capture.leader.pid)


def capture_session(leader: OwnedProcess) -> SessionCapture:
    """Capture the session `leader` leads before anything is signalled.

    No members when `leader` is no longer the live shell.
    """
    capture = SessionCapture(leader, [], None)
    members: dict[int, _Member] = {}
    _pin_roots(leader, (), members, freeze=False)
    if leader.pid in members:
        _absorb(capture, members, _scan())
    return capture


def refresh(captures: Iterable[SessionCapture]) -> bool:
    """Fold every session's newcomers into its capture; True while any session holds a process.

    One scan serves every capture — also one with no live process and no
    fresh proof, which can take nothing more: a process still in its session
    is logged (pid and command name) and keeps it busy, never signalled.
    """
    pinned: list[tuple[SessionCapture, dict[int, _Member]]] = []
    for capture in captures:
        members: dict[int, _Member] = {}
        _pin_roots(capture.leader, capture.members, members, freeze=False)
        pinned.append((capture, members))
    if not pinned:
        return False
    table = _scan()
    busy = False
    for capture, members in pinned:
        busy = _absorb(capture, members, table) or busy
    return busy


def terminate(members: Iterable[OwnedProcess], sig: int = signal.SIGTERM) -> None:
    """Send `sig` (SIGTERM by default) to each member still the captured process."""
    for identity in members:
        process = _pin_identity(identity)
        if process is not None:
            with contextlib.suppress(psutil.Error):
                process.send_signal(sig)


def kill_session_tree(
    leader: OwnedProcess,
    *,
    also: Iterable[OwnedProcess] = (),
    wait_s: float,
    proven_at: float | None = None,
) -> TreeKill:
    """Freeze, then SIGKILL, the whole membership of `leader`'s session.

    `leader` is the session's shell. `also` adds roots captured earlier (a
    graceful kill's pre-TERM snapshot, a stop's capture), so their trees are
    still taken when the shell itself already died, and any of them still in
    the session proves its id; `proven_at` carries a caller's own proof
    (`SessionCapture.proven_at`). The leader dies last, after the rest were
    waited for; `wait_s` bounds each wait. When anything raises midway, every
    member frozen and not yet killed is SIGKILLed on the way out.
    """
    members: dict[int, _Member] = {}
    done: set[OwnedProcess] = set()
    try:
        return _kill_frozen(leader, also, members, done, wait_s, _Proof(proven_at))
    finally:
        _kill_stranded(members.values(), done)


def _kill_frozen(
    leader: OwnedProcess,
    also: Iterable[OwnedProcess],
    members: dict[int, _Member],
    done: set[OwnedProcess],
    wait_s: float,
    proof: _Proof,
) -> TreeKill:
    _pin_roots(leader, also, members, freeze=True)
    outcome = _Outcome()
    # Two rounds: the second closure runs while the frozen shell (or a member
    # the first batch could not end) still proves the session id, and takes
    # whatever ran or forked while the first batch was dying.
    for _ in range(2):
        if not _close(members, leader, proof):
            logger.warning(
                "pty session {leader}: still finding new members after {passes} freeze "
                "passes; a process forked during the last pass may escape the kill",
                leader=leader.pid,
                passes=_MAX_FREEZE_PASSES,
            )
        body = [
            member
            for member in reversed(members.values())
            if member.identity != leader and member.identity not in done
        ]
        outcome.kill(body, done, wait_s)
    shell = members.get(leader.pid)
    outcome.kill([shell] if shell is not None and shell.identity == leader else [], done, wait_s)
    return outcome.result()
