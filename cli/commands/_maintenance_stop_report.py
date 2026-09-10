"""Deadline survivor reporting for the held stop path (issue #2162).

When `cli.commands._maintenance_stop.stop_services` runs its deadline out, the
operator needs the exact resource that remained — not a bare pid list. This
module builds that report: per-process identity (owning session, role, birth
pair, cmdline), the recorded process groups still occupied, and the stage the
stop died in. The caller raises it as `StopIncompleteError`; the lifecycle
journal keeps the same payload as JSON right beside the printable message.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass

import psutil

from shared.proc_tree import OwnedProcess
from shared.session_record import SessionRecord, pid_starttime_ticks


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
        return process.create_time() == identity.birth and process.status() not in (
            psutil.STATUS_ZOMBIE,
            psutil.STATUS_DEAD,
        )
    except (psutil.Error, OSError):
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


def _group_member_candidates(pgid: int) -> list[OwnedProcess]:
    """Best-effort live members of `pgid` for the deadline report.

    Unlike `_group_members` (whose answer gates signalling), a member that
    cannot be inspected is omitted here rather than refusing the whole report —
    the group id itself stays in the message either way.
    """
    candidates: list[OwnedProcess] = []
    for process in psutil.process_iter():
        try:
            if process.status() in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD):
                continue
            if os.getpgid(process.pid) != pgid:
                continue
            candidates.append(OwnedProcess.capture(process))
        except (psutil.Error, OSError):
            continue
    return candidates


def service_inventory(
    *,
    records: dict[str, SessionRecord],
    leaders: dict[str, OwnedProcess],
    by_service: dict[str, set[OwnedProcess]],
    tracked: set[OwnedProcess],
    groups: tuple[int, ...],
) -> SurvivorInventory:
    """Resolve a deadline's remaining tree to per-process identity (issue #2162).

    Answers "what held the stop" for the operator: which recorded session each
    survivor belongs to — its leader, a captured descendant, or a member of the
    session's recorded process group that appeared after the capture — plus the
    birth pair and cmdline to act on. Reads only; nothing here signals.
    """
    service_of: dict[int, str] = {}
    for name, tree in by_service.items():
        for identity in tree:
            service_of.setdefault(identity.pid, name)
    leader_pids = {identity.pid for identity in leaders.values()}
    group_owner: dict[int, str] = {}
    if groups and os.name == "posix":
        from shared.posixproc import _pgid_of

        for name, record in records.items():
            if _identity_matches(leaders[name]):
                try:
                    group = _pgid_of(psutil.Process(record.pid))
                except (psutil.Error, OSError):
                    continue
            else:
                group = record.pgid
            if group is not None:
                group_owner.setdefault(group, name)

    survivors: list[StopSurvivor] = []
    seen: set[int] = set()
    for identity in live_identities(tracked):
        survivors.append(
            capture_survivor(
                identity,
                service=service_of.get(identity.pid),
                role="leader" if identity.pid in leader_pids else "descendant",
            )
        )
        seen.add(identity.pid)
    occupied = occupied_groups(groups)
    for group in occupied:
        for member in _group_member_candidates(group):
            if member.pid in seen:
                continue
            survivors.append(
                capture_survivor(
                    member,
                    service=group_owner.get(group),
                    role="group-member",
                    pgid_hint=group,
                )
            )
            seen.add(member.pid)
    return SurvivorInventory(
        survivors=sorted(survivors, key=lambda survivor: survivor.pid), groups=occupied
    )
