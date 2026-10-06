"""Bounded best-effort terminal closure using explicitly known process groups.

Closing a terminal attempts HUP, TERM and KILL, then reports known shell/job
identities still alive. It never discovers descendants, scans the host process
table or certifies that every process originally launched from the terminal is
gone. Residual host processes belong to operational investigation.
"""

from __future__ import annotations

import signal
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import psutil

from base.native_process.ownership import OwnedProcess
from base.sessions.pty import process_groups


@dataclass(frozen=True)
class Target:
    """One shell to close: its session name and the identity recorded at birth.

    `members` are explicitly recorded job identities. They are cleanup targets,
    not a complete inventory of the session or its descendants.
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
    """A known cleanup target still alive after the bounded signaling attempt."""

    session: str
    process: OwnedProcess
    role: str  # "terminal" (the shell) or "job" (any other member)


@dataclass(frozen=True)
class Outcome:
    """Closed busy terminals and known survivors; no descendant-clearance proof."""

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


def _known(target: Target) -> tuple[OwnedProcess, ...]:
    return tuple(dict.fromkeys((target.shell, *target.members)))


def _present(identities: Iterable[OwnedProcess]) -> list[OwnedProcess]:
    return [identity for identity in identities if process_groups.live(identity)]


def _named(identities: Iterable[OwnedProcess]) -> tuple[tuple[int, str], ...]:
    names: list[tuple[int, str]] = []
    for identity in identities:
        if not process_groups.live(identity):
            continue
        try:
            name = psutil.Process(identity.pid).name()
        except psutil.NoSuchProcess:
            continue
        if process_groups.live(identity):
            names.append((identity.pid, name))
    return tuple(names)


def close_sessions(targets: Iterable[Target], *, grace_s: float, kill_s: float) -> Outcome:
    """Attempt closure of known groups within shared grace and kill budgets.

    A gone/recycled identity is skipped. Concrete signaling/identity errors
    propagate. A known survivor is diagnostic evidence, not a host-wide scan.
    """
    entries = list(targets)
    known = tuple(dict.fromkeys(identity for target in entries for identity in _known(target)))
    process_groups.signal((target.shell for target in entries), signal.SIGHUP)
    process_groups.signal(
        (member for target in entries for member in target.members if member != target.shell),
        signal.SIGTERM,
    )
    remaining = process_groups.wait(known, grace_s)
    if remaining:
        process_groups.signal(remaining, signal.SIGKILL)
        process_groups.wait(remaining, kill_s)
    survivors: list[Survivor] = []
    closed: list[ClosedSession] = []
    for target in entries:
        left = _present(_known(target))
        survivors.extend(
            Survivor(target.name, identity, "terminal" if identity == target.shell else "job")
            for identity in left
        )
        if target.members and not process_groups.live(target.shell):
            closed.append(ClosedSession(target.name, target.shell, _named(left)))
    return Outcome(tuple(closed), tuple(survivors))
