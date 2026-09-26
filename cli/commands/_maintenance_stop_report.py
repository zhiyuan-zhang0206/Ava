"""Native survivor diagnostics for terminal stop and maintenance waits.

Reports retain the captured birth even when live facts cannot be read. The
terminal boundary raises StopIncompleteError; its lifecycle journal stores the
same structured inventory. Process groups are observation-only facts.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass

import psutil

from shared.native_process import pid_starttime_ticks
from shared.native_process.ownership import OwnedProcess


def occupied_groups(groups: tuple[int, ...]) -> list[int]:
    """Which of `groups` still hold live members, by scanning membership.

    `killpg(..., 0)` can report EPERM for an empty group on macOS, so read the
    actual membership instead; an unreadable member cannot certify emptiness.
    """
    if not groups:
        return []
    occupied: set[int] = set()
    for process in psutil.process_iter():
        try:
            group = os.getpgid(process.pid)
            if group in groups and process.status() not in (
                psutil.STATUS_ZOMBIE,
                psutil.STATUS_DEAD,
            ):
                occupied.add(group)
        except (psutil.NoSuchProcess, ProcessLookupError):
            continue
    return sorted(occupied)


class StopIncompleteError(TimeoutError):
    """A held stop's deadline expired with owned processes still alive.

    The message carries the operator-readable inventory; `survivors` carries
    the same entries as JSON-safe dicts for the durable journal, and `stage`
    names the stop phase that hit the deadline (issue #2162).
    """

    def __init__(
        self,
        message: str,
        *,
        stage: str | None = None,
        survivors: list[dict[str, object]] | None = None,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.survivors: list[dict[str, object]] = list(survivors or [])


@dataclass(frozen=True)
class StopSurvivor:
    """One process still alive when a stop's deadline expired.

    `birth`/`starttime` are the captured identity pair — the same proof the
    stop path revalidates before signalling; the live fields are read at
    report time and are best-effort, so an unreadable process is reported as
    unreadable rather than dropped.
    """

    pid: int
    role: str
    service: str | None
    ppid: int | None
    pgid: int | None
    status: str | None
    birth: float
    starttime: int | None
    cmdline: str | None

    def render(self) -> str:
        parts = [f"pid={self.pid}"]
        if self.ppid is not None:
            parts.append(f"ppid={self.ppid}")
        if self.pgid is not None:
            parts.append(f"pgid={self.pgid}")
        parts.append(f"role={self.role}")
        parts.append(f"service={self.service!r}" if self.service else "service=<unattributed>")
        if self.status is not None:
            parts.append(f"status={self.status}")
        parts.append(f"birth={self.birth:.3f}")
        if self.starttime is not None:
            parts.append(f"starttime={self.starttime}")
        if self.cmdline:
            text = self.cmdline
            if len(text) > 300:
                text = text[:297] + "..."
            parts.append(f"cmdline={text!r}")
        else:
            parts.append("cmdline=<unreadable>")
        return "    · " + " ".join(parts)

    def payload(self) -> dict[str, object]:
        return {
            "pid": self.pid,
            "role": self.role,
            "service": self.service,
            "ppid": self.ppid,
            "pgid": self.pgid,
            "status": self.status,
            "birth": self.birth,
            "starttime": self.starttime,
            "cmdline": self.cmdline,
        }


@dataclass(frozen=True)
class SurvivorInventory:
    """A held stop's deadline report: who is left, and in which recorded group."""

    survivors: list[StopSurvivor]
    groups: list[int]

    def render(self, *, stage: str) -> str:
        header = f"remaining process inventory (stage={stage}; nothing was force-killed):"
        if not self.survivors and not self.groups:
            return f"{header} no survivor readable at the deadline"
        lines = [header, *(survivor.render() for survivor in self.survivors)]
        if self.groups:
            lines.append(f"  occupied recorded process groups: {sorted(self.groups)}")
        lines.append(
            "  recovery: retry the command to reconverge recorded groups, or escalate "
            "explicitly with `ava stop --force` if the listed process(es) are expendable."
        )
        return "\n".join(lines)

    def payload(self) -> list[dict[str, object]]:
        return [survivor.payload() for survivor in self.survivors]


def live_identities(identities: Iterable[OwnedProcess]) -> list[OwnedProcess]:
    """Captured identities still present, in pid order.

    Reporting must not drop a survivor it cannot verify: an identity whose
    birth cannot be re-read counts as present (it is then listed without live
    facts), while a confirmed birth mismatch counts as gone — the same answer
    signalling gives.
    """
    present: list[OwnedProcess] = []
    for identity in identities:
        try:
            alive = identity.live()
        except Exception:  # fail-fast-ok: the deadline report must not raise
            alive = True
        if alive:
            present.append(identity)
    return sorted(present, key=lambda identity: identity.pid)


def _identity_matches(identity: OwnedProcess) -> bool:
    """Whether `identity` is still the process it was captured as.

    Guards the report's live reads exactly as signal delivery is guarded: a
    PID recycled since capture must not describe itself with a stranger's
    cmdline. An unconfirmed birth (the platform cannot re-read it) reads as
    not matched; the entry then keeps the captured identity and no live facts.
    """
    try:
        if identity.starttime is not None:
            return pid_starttime_ticks(identity.pid) == identity.starttime
        process = psutil.Process(identity.pid)
        return identity.birth_matches(process) and process.status() not in (
            psutil.STATUS_ZOMBIE,
            psutil.STATUS_DEAD,
        )
    except (psutil.Error, OSError, RuntimeError):
        return False


def capture_survivor(
    identity: OwnedProcess, *, service: str | None, role: str, pgid_hint: int | None = None
) -> StopSurvivor:
    """Read one survivor's live facts; reads only, never signals, never raises."""
    ppid: int | None = None
    pgid = pgid_hint
    status: str | None = None
    cmdline: str | None = None
    if _identity_matches(identity):
        try:
            process = psutil.Process(identity.pid)
            ppid = process.ppid()
            status = process.status()
            cmdline = " ".join(process.cmdline()) or None
        except (psutil.Error, OSError):
            ppid = status = cmdline = None
        if pgid is None and os.name == "posix":
            try:
                pgid = os.getpgid(identity.pid)
            except OSError:
                pgid = None
    return StopSurvivor(
        pid=identity.pid,
        role=role,
        service=service,
        ppid=ppid,
        pgid=pgid,
        status=status,
        birth=identity.birth,
        starttime=identity.starttime,
        cmdline=cmdline,
    )
