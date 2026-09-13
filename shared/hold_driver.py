"""The shepherd identity a maintenance hold is bound to (task #3270).

A maintenance hold -- the `pause_owner` journal's maintenance payload -- is
taken by an operator-side ladder that may span several `ava maintenance`
commands (`prepare` -> `drain` -> ... -> `resume`) or a script driving the CLI.
The hold must be released, or loudly escalated, when the process shepherding
that ladder exits or dies, and it must NOT be judged abandoned while a live
shepherd remains. This module records and judges that shepherd.

**The binding is the topmost process of the writer's command tree below its
session leader** (the 2026-09-13 ruling by agent #2343/#405's thread):

- A session id survives reparenting (`os.getsid` -- the issue #2331 membership
  route), so a one-shot `sh -c` intermediary is never mistaken for the owner:
  it sits below the root of the tree and its exit changes nothing.
- In a persistent session used as a multi-command flow, the session leader
  itself is the root (there is nothing between the command and the leader), so
  the binding lives exactly as long as the session: the flow is shepherded
  across commands.
- A script that drives the ladder directly (`subprocess.run("ava ...")` or
  through a relay shell) IS the root, so its exit -- and nothing weaker -- ends
  the binding.
- Without a readable session leader (Windows has no `getsid`; a leaderless
  session) the direct parent is the fallback, per the same ruling.

The identity is a pid + birth pair judged with the discipline
`shared/proc_tree.py` owns (the same key pid reuse is judged by). Missing
evidence -- no identity recorded (a pre-#3270 journal) or an unreadable probe
-- is never a release license; the callers escalate loudly instead.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal, cast

import psutil

from shared.proc_tree import create_time_matches, stable_create_time
from shared.session_record import pid_starttime_ticks

DriverLiveness = Literal["alive", "dead", "missing", "unreadable"]

# The evidence recorded for humans reading the journal (`ava maintenance
# status`): the first couple of argv elements, bounded. Identity never depends
# on argv -- it is the pid + birth pair -- this is only what the operator sees.
_ARGV_HEAD = 120

# Argument count folded into the evidence string; enough to name the entry
# module/script without copying an entire command line into the journal.
_ARGV_PARTS = 3


@dataclass(frozen=True)
class ProcessRef:
    """One recorded process identity: pid + birth (+ argv evidence)."""

    pid: int
    birth: float
    starttime: int | None
    argv: str

    def probe(self) -> Literal["live", "gone", "unreadable"]:
        """Whether the process this ref names is still the same live process.

        `gone` is the unambiguous reading (no such pid, or a recycled pid whose
        birth does not match); `unreadable` means the question could not be
        answered and must not be treated as death.
        """
        try:
            proc = psutil.Process(self.pid)
        except psutil.NoSuchProcess:
            return "gone"
        except psutil.Error:
            return "unreadable"
        try:
            if self.starttime is not None:
                actual = pid_starttime_ticks(self.pid)
                if actual is None:
                    return "unreadable"
                if actual != self.starttime:
                    return "gone"
            elif not create_time_matches(stable_create_time(proc), self.birth):
                return "gone"
            if proc.status() in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD):
                return "gone"
        except psutil.NoSuchProcess:
            return "gone"
        except (psutil.Error, OSError):
            return "unreadable"
        return "live"

    def encode(self) -> dict[str, object]:
        return {
            "pid": self.pid,
            "birth": self.birth,
            "starttime": self.starttime,
            "argv": self.argv,
        }

    @classmethod
    def decode(cls, value: object) -> ProcessRef:
        if not isinstance(value, dict):
            raise TypeError("driver process reference must be an object")
        raw = cast("dict[str, object]", value)
        pid = raw["pid"]
        birth = raw["birth"]
        starttime = raw.get("starttime")
        argv = raw.get("argv", "")
        if type(pid) is not int:
            raise TypeError("driver process reference pid must be an int")
        if pid <= 0:
            raise ValueError("driver process reference pid must be positive")
        if isinstance(birth, bool) or not isinstance(birth, (int, float)):
            raise TypeError("driver process reference birth must be a number")
        if starttime is not None and type(starttime) is not int:
            raise TypeError("driver process reference starttime must be an int or null")
        if not isinstance(argv, str):
            raise TypeError("driver process reference argv must be a string")
        return cls(pid=pid, birth=float(birth), starttime=starttime, argv=argv)


@dataclass(frozen=True)
class HoldDriver:
    """The recorded shepherd: `root` decides liveness, `leader` is evidence.

    `root` is the topmost process of the writer's command tree below its
    session leader (or the session leader itself, or the direct parent when no
    leader is readable); `leader` is the session leader at mint time, kept for
    the operator-facing journal. None means the identity was not recorded.
    """

    root: ProcessRef | None = None
    leader: ProcessRef | None = None

    def encode(self) -> dict[str, object]:
        return {
            "root": self.root.encode() if self.root is not None else None,
            "leader": self.leader.encode() if self.leader is not None else None,
        }

    @classmethod
    def decode(cls, value: object) -> HoldDriver:
        if not isinstance(value, dict):
            raise TypeError("hold driver must be an object")
        raw = cast("dict[str, object]", value)
        root = raw.get("root")
        leader = raw.get("leader")
        return cls(
            root=None if root is None else ProcessRef.decode(root),
            leader=None if leader is None else ProcessRef.decode(leader),
        )


def _argv_head(proc: psutil.Process) -> str:
    try:
        parts: list[str] = list(proc.cmdline()[:_ARGV_PARTS])
    except (psutil.Error, OSError):
        return ""
    return " ".join(parts)[:_ARGV_HEAD]


def _capture(proc: psutil.Process) -> ProcessRef | None:
    """Best-effort capture of one live process; None when unreadable."""
    try:
        birth = stable_create_time(proc)
    except (psutil.Error, OSError):
        return None
    return ProcessRef(
        pid=proc.pid,
        birth=birth,
        starttime=pid_starttime_ticks(proc.pid),
        argv=_argv_head(proc),
    )


def _leader_ref(sid: int | None) -> ProcessRef | None:
    if sid is None or sid <= 0:
        return None
    try:
        return _capture(psutil.Process(sid))
    except (psutil.Error, OSError):
        return None


def _resolve_root(
    ancestors: list[psutil.Process], sid: int | None, leader: ProcessRef | None
) -> ProcessRef | None:
    """The topmost ancestor below the session leader, per the module docstring."""
    if sid is not None:
        below: list[psutil.Process] = []
        for ancestor in ancestors:
            if ancestor.pid == sid:
                break
            below.append(ancestor)
        else:
            # The leader is not in this process's ancestry (it exited, or the
            # member was reparented out of the session): the direct parent is
            # the recorded fallback; nothing above it is ours to claim.
            below = ancestors[:1]
        if below:
            return _capture(below[-1])
        return leader
    return _capture(ancestors[0]) if ancestors else None


def mint_driver() -> HoldDriver:
    """Record the shepherding identity of the CALLING process, best-effort.

    Called by the operator-side entrypoints that take or advance a maintenance
    hold (`ava maintenance ...` verbs, the local stop/pause flow). Nothing here
    raises: an unreadable environment mints an empty identity, which readers
    treat as missing evidence -- loud, never a release license.
    """
    sid: int | None = None
    if os.name == "posix" and hasattr(os, "getsid"):
        try:
            sid = os.getsid(0)
        except OSError:
            sid = None
    try:
        ancestors = psutil.Process().parents()
    except psutil.Error:
        ancestors = []
    leader = _leader_ref(sid)
    try:
        root = _resolve_root(ancestors, sid, leader)
    except psutil.Error:
        root = None
    return HoldDriver(root=root, leader=leader)


def liveness(driver: HoldDriver | None) -> DriverLiveness:
    """Judge the recorded shepherd: alive / dead / missing / unreadable.

    `missing` is definitional (no identity was recorded -- a pre-#3270
    journal); `unreadable` is a probe that could not answer. Only `dead` -- an
    unambiguous birth-checked absence -- may license an automatic release.
    """
    if driver is None or driver.root is None:
        return "missing"
    reading = driver.root.probe()
    if reading == "live":
        return "alive"
    if reading == "gone":
        return "dead"
    return "unreadable"
