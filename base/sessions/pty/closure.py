"""Closing persistent shells for good: hang up, a bounded grace, SIGKILL, evidence.

The one terminal closure. A stop, the service's own shutdown and the sweep of a
crashed service's leftovers all end sessions through `close_sessions`; the
pty-sessions service runs it for the `close_all` request because it holds the
masters, and the caller (`ava stop`) turns its `Outcome` into owner notices.

Each shell's whole session is captured as `session_tree` defines it (the shell,
its descendants and every process of its POSIX session, each pinned by birth)
before the first signal. Shells get SIGHUP first, so a restart loop cannot keep
producing jobs; every other member gets SIGTERM; whatever is still alive when
the grace ends is SIGKILLed with its session whole
(decisions/2026-09-28-stop-escalates-to-sigkill.md). A session with anything
beyond its shell at capture is busy; a busy session whose shell the closure
verified gone is `ClosedSession` (with the processes of it that outlived the
SIGKILL), and every process that outlived the SIGKILL is a `Survivor`.
"""

from __future__ import annotations

import signal
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import psutil

from base.native_process.ownership import OwnedProcess
from base.sessions.pty import session_tree


@dataclass(frozen=True)
class Target:
    """One shell to close: its session name and the identity recorded at birth.

    `members` are processes of the session recorded earlier. They matter only
    when the shell itself is already gone (a crashed service's leftovers): a
    recorded member that is still alive proves the session id and is taken with
    its whole tree.
    """

    name: str
    shell: OwnedProcess
    members: tuple[OwnedProcess, ...] = ()


@dataclass(frozen=True)
class ClosedSession:
    """A busy session whose shell is verified gone.

    `left` are the session's processes that outlived the SIGKILL, as
    (pid, command name); empty when everything of it is gone.
    """

    name: str
    shell: OwnedProcess
    left: tuple[tuple[int, str], ...] = ()


@dataclass(frozen=True)
class Survivor:
    """A captured process that outlived its SIGKILL, with the session it belongs to."""

    session: str
    process: OwnedProcess
    role: str  # "terminal" (the shell) or "job" (any other member)


@dataclass(frozen=True)
class Outcome:
    """What a closure did: the busy sessions it closed and what outlived it."""

    closed: tuple[ClosedSession, ...] = ()
    survivors: tuple[Survivor, ...] = ()

    def to_wire(self) -> dict[str, Any]:
        return {
            "closed": [
                {
                    "name": closed.name,
                    "shell": _identity_wire(closed.shell),
                    "left": [list(pair) for pair in closed.left],
                }
                for closed in self.closed
            ],
            "survivors": [
                {
                    "session": survivor.session,
                    "process": _identity_wire(survivor.process),
                    "role": survivor.role,
                }
                for survivor in self.survivors
            ],
        }

    @classmethod
    def from_wire(cls, data: dict[str, Any]) -> Outcome:
        return cls(
            closed=tuple(
                ClosedSession(
                    item["name"],
                    identity_from_wire(item["shell"]),
                    tuple((int(pid), str(name)) for pid, name in item["left"]),
                )
                for item in data["closed"]
            ),
            survivors=tuple(
                Survivor(item["session"], identity_from_wire(item["process"]), item["role"])
                for item in data["survivors"]
            ),
        )


def _identity_wire(identity: OwnedProcess) -> dict[str, Any]:
    return {"pid": identity.pid, "birth": identity.birth, "starttime": identity.starttime}


def identity_from_wire(item: dict[str, Any]) -> OwnedProcess:
    starttime = item["starttime"]
    return OwnedProcess(
        int(item["pid"]), float(item["birth"]), None if starttime is None else int(starttime)
    )


@dataclass
class _Entry:
    """One session the closure takes, captured before any signal."""

    name: str
    capture: session_tree.SessionCapture
    busy: bool

    @property
    def shell(self) -> OwnedProcess:
        return self.capture.leader

    def role(self, identity: OwnedProcess) -> str:
        return "terminal" if identity == self.shell else "job"


def _present(identities: Iterable[OwnedProcess]) -> list[OwnedProcess]:
    """The identities still present, in pid order.

    One that cannot be verified counts as present (it is reported, never
    certified gone); a confirmed birth mismatch counts as gone.
    """
    present: list[OwnedProcess] = []
    for identity in identities:
        try:
            alive = identity.live()
        except Exception:  # evidence gathering must not raise
            alive = True
        if alive:
            present.append(identity)
    return sorted(present, key=lambda identity: identity.pid)


def _capture(targets: Iterable[Target]) -> list[_Entry]:
    entries: list[_Entry] = []
    for target in targets:
        capture = session_tree.capture_session(target.shell)
        if not capture.members and target.members:
            # The shell is gone; recorded members that still live keep the session open.
            capture = session_tree.SessionCapture(
                target.shell, [target.shell, *target.members], None
            )
            session_tree.refresh([capture])
        if not _present(capture.members):
            continue  # nothing of the session is left to close
        busy = bool(_present(member for member in capture.members if member != target.shell))
        entries.append(_Entry(target.name, capture, busy))
    return entries


def _hang_up(entries: list[_Entry]) -> None:
    """SIGHUP every shell, then SIGTERM every other captured member.

    The shells go first: an interactive shell's own SIGHUP makes bash exit
    (re-sending HUP to its jobs), so a loop that restarts its job cannot keep
    producing new descendants during the grace (#2045).
    """
    session_tree.terminate([entry.shell for entry in entries], signal.SIGHUP)
    for entry in entries:
        session_tree.terminate(set(entry.capture.members) - {entry.shell})


def _await_members(entries: list[_Entry], until: float) -> bool:
    """Wait for every captured member to exit; False when `until` passes first.

    Each poll folds each session's newcomers into its capture
    (`session_tree.refresh`). A member can fork while the poll that finds it
    gone is still scanning, so a quiet poll only counts once a second one,
    whose scan began after every member was gone, is quiet too.
    """
    captures = [entry.capture for entry in entries]
    quiet = False
    while True:
        if not session_tree.refresh(captures):
            if quiet:
                return True
            quiet = True
            continue
        quiet = False
        left = until - time.monotonic()
        if left <= 0:
            return False
        time.sleep(min(0.05, left))


def _kill_leftovers(entries: list[_Entry], wait_s: float) -> list[tuple[_Entry, OwnedProcess]]:
    """SIGKILL each session's remaining membership; return what outlived it.

    Every session dies through `session_tree.kill_session_tree`: frozen, killed
    children first with the shell last, rooted at the shell and at every member
    captured, so a job whose shell already exited is still taken with its
    descendants. One last refresh first, so every kill starts from its
    session's newest capture and proof; a session that can yield nothing more
    is skipped.
    """
    session_tree.refresh(entry.capture for entry in entries)
    survivors: list[tuple[_Entry, OwnedProcess]] = []
    for entry in entries:
        capture = entry.capture
        if not capture.active:
            continue
        result = session_tree.kill_session_tree(
            capture.leader, also=capture.members, wait_s=wait_s, proven_at=capture.proven_at
        )
        survivors += [(entry, identity) for identity in result.survivors]
    return survivors


def _named(identities: list[OwnedProcess]) -> tuple[tuple[int, str], ...]:
    """(pid, command name) of each process still running as its captured identity."""
    named: list[tuple[int, str]] = []
    for identity in sorted(identities, key=lambda identity: identity.pid):
        try:
            name = psutil.Process(identity.pid).name()
        except psutil.NoSuchProcess:
            continue
        except psutil.Error:
            name = "<unreadable>"
        if _present([identity]):  # the name was read from that process
            named.append((identity.pid, name))
    return tuple(named)


def _closed(
    entries: list[_Entry], killed: list[tuple[_Entry, OwnedProcess]]
) -> tuple[ClosedSession, ...]:
    """Every busy session whose shell is verified gone, with what of it outlived the SIGKILL.

    The shell is the session as its owner uses it: once it is gone the session
    cannot be used again, so it counts as closed even when a process of it
    outlived the SIGKILL (the notice names it) and when another session keeps
    the closure incomplete (issue #2044's "notify only what actually closed",
    judged by the shell). A session whose shell still lives is not closed; a
    retry sees it again.
    """
    stuck = set(_present(identity for _entry, identity in killed))
    left: dict[str, list[OwnedProcess]] = {}
    for entry, identity in killed:
        if identity in stuck:
            left.setdefault(entry.name, []).append(identity)
    return tuple(
        ClosedSession(entry.name, entry.shell, _named(left.get(entry.name, [])))
        for entry in entries
        if entry.busy and not _present([entry.shell])
    )


def close_sessions(targets: Iterable[Target], *, grace_s: float, kill_s: float) -> Outcome:
    """Close every target: hang up, wait up to `grace_s`, SIGKILL what is left.

    The SIGKILL leg runs even when `grace_s` is zero, each of its waits bounded
    by `kill_s`. Nothing is signalled before every session is captured. A
    target whose shell is no longer the live recorded process is skipped.
    """
    entries = _capture(targets)
    _hang_up(entries)
    graceful = _await_members(entries, time.monotonic() + grace_s)
    killed = [] if graceful else _kill_leftovers(entries, kill_s)
    survivors = tuple(
        Survivor(entry.name, identity, entry.role(identity))
        for entry, identity in killed
        if _present([identity])
    )
    return Outcome(_closed(entries, killed), survivors)
