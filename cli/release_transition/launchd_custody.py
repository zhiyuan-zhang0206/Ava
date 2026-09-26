"""Records and job-group closure for the macOS finite executor job.

launchd's own cleanup is one SIGTERM to the job process group when the helper
exits; a member that ignores or handles SIGTERM survives it. The finite helper
therefore closes its group itself while it still leads it, and publishes the
group (PID, PGID, audit session) before any spawn. This module turns those
facts into closure: recorded births not live and the group provably empty. A
member that outlived both recorded owners cannot be proven to belong to this
attempt, so it is never signalled; closure then refuses with its evidence.
"""

from __future__ import annotations

import os
import signal
import stat
import time
from pathlib import Path
from typing import Literal, Self

import psutil
from pydantic import Field, JsonValue, ValidationError, model_validator

from cli.release_transition.native import DARWIN
from cli.release_transition.request import Record
from services.permissions_helper.finite_artifact import HelperArtifact
from shared.native_process.ownership import OwnedProcess
from shared.runtime_release import ReleaseRejectedError
from shared.verified_file import regular_bytes

_CLOSURE_WAIT_S = 5.0
_KILL_WAIT_S = 5.0
_KILL_ROUNDS = 5
_POLL_S = 0.05
_EVIDENCE_MEMBERS = 8
Evidence = Literal["launchd", "boot-changed", "domain-lost"]


class Birth(Record):
    """One captured native process birth (psutil monotonic start on macOS)."""

    pid: int = Field(gt=1)
    birth: float
    starttime: int | None

    @classmethod
    def of(cls, process: OwnedProcess) -> Birth:
        return cls(pid=process.pid, birth=process.birth, starttime=process.starttime)

    def owned(self) -> OwnedProcess:
        return OwnedProcess(self.pid, self.birth, self.starttime)


class DarwinLaunch(Record):
    kind: Literal["darwin-launchd-v1"] = DARWIN
    operation: str
    attempt: int = Field(ge=0)
    home: str
    registry: str
    label: str
    domain: str
    uid: int = Field(ge=0)
    boot_id: str
    macos_product: str
    macos_build: str
    plist: str
    plist_sha256: str
    stdout: str
    stderr: str
    group_receipt: str
    helper: HelperArtifact
    artifact_digest: str
    manifest_digest: str
    runtime_root: str
    interpreter: str
    cwd: str
    argv: list[str]
    environment: dict[str, str]
    exit_timeout: int = Field(gt=0)

    @property
    def target(self) -> str:
        return f"{self.domain}/{self.label}"

    def program_arguments(self) -> list[str]:
        """Fixed helper argv; the helper builds the executor environment from it alone."""
        pairs = [
            part
            for key in sorted(self.environment)
            for part in ("--env", f"{key}={self.environment[key]}")
        ]
        return [
            self.helper.executable,
            "--finite-executor",
            "v1",
            "--cwd",
            self.cwd,
            "--group-receipt",
            self.group_receipt,
            *pairs,
            "--",
            *self.argv,
        ]


class NativeReceipt(Record):
    """Recorded by the executor before any effect: attempt, helper and executor births."""

    kind: Literal["darwin-launchd-v1"] = DARWIN
    label: str
    domain: str
    boot_id: str
    asid: int
    pgid: int = Field(gt=1)
    helper: Birth
    executor: Birth


class GroupReceipt(Record):
    """Published by the finite helper before its only spawn; absent means nothing spawned."""

    finite_executor: Literal["v1"]
    helper_pid: int = Field(gt=1)
    pgid: int = Field(gt=1)
    asid: int = Field(ge=0)

    @model_validator(mode="after")
    def leader(self) -> Self:
        if self.helper_pid != self.pgid:
            raise ValueError("finite helper must lead its launchd job process group")
        return self


class DarwinJob(Record):
    """Native readback, not a declaration that the release transition succeeded.

    ``evidence`` names the source of a terminal: launchd's own facts, or the end
    of the recorded boot or login domain (launchd then holds no facts at all).
    """

    kind: Literal["darwin-launchd-v1"] = DARWIN
    label: str
    domain: str
    boot_id: str
    evidence: Evidence
    asid: int | None
    state: Literal["running", "not running"]
    runs: int | None
    helper: Birth | None
    executor: Birth | None
    pgid: int | None
    exit_code: int | None
    signal: int | None
    closed: dict[str, Birth] | None

    @property
    def finished(self) -> bool:
        return self.state == "not running" and self.helper is None

    @property
    def identity(self) -> dict[str, JsonValue]:
        """Helper and executor births stay distinct; neither substitutes for the other."""
        if self.helper is None or self.executor is None or self.pgid is None or self.asid is None:
            raise RuntimeError("only a running executor can supply a native birth receipt")
        return NativeReceipt(
            label=self.label,
            domain=self.domain,
            boot_id=self.boot_id,
            asid=self.asid,
            pgid=self.pgid,
            helper=self.helper,
            executor=self.executor,
        ).model_dump(mode="json")


def read_group_receipt(launch: DarwinLaunch) -> GroupReceipt | None:
    """The helper's pre-spawn group receipt; a private, whole file or nothing."""
    path = Path(launch.group_receipt)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != launch.uid
        or info.st_mode & 0o077
        or info.st_nlink != 1
    ):
        raise RuntimeError("finite helper group receipt is not a private file; custody retained")
    try:
        return GroupReceipt.model_validate_json(regular_bytes(path, max_bytes=4096))
    except (ReleaseRejectedError, ValidationError) as exc:
        raise RuntimeError("finite helper group receipt is unreadable; custody retained") from exc


def require_consistent(receipt: NativeReceipt | None, group: GroupReceipt | None) -> None:
    """The executor receipt and the helper's group receipt describe one group."""
    if receipt is None:
        return
    if group is None:
        raise RuntimeError(
            "executor receipt exists without the helper's pre-spawn group receipt; "
            "custody unresolved"
        )
    if (receipt.pgid, receipt.helper.pid, receipt.asid) != (
        group.pgid,
        group.helper_pid,
        group.asid,
    ):
        raise RuntimeError("helper group receipt differs from the executor receipt")


def _group_exists(pgid: int) -> bool:
    """``killpg(0)`` is atomic; EPERM still names an existing group (zombie or foreign member)."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def group_empty(pgid: int, helper: Birth | None) -> bool:
    """Whether the group of a helper launchd already reaped is gone.

    An empty group cannot be rejoined. XNU never allocates a PID equal to a
    live process-group id, so a different, live birth at the recorded helper's
    PID proves the original group emptied first.
    """
    if not _group_exists(pgid):
        return True
    if helper is None or helper.owned().live():
        return False
    try:
        return psutil.Process(pgid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def group_members(pgid: int) -> tuple[set[OwnedProcess], set[int]]:
    """Captured live members of ``pgid`` and PIDs whose birth could not be read."""
    members: set[OwnedProcess] = set()
    unreadable: set[int] = set()
    for process in psutil.process_iter():
        try:
            if os.getpgid(process.pid) != pgid:
                continue
            member = OwnedProcess.capture(psutil.Process(process.pid))
            if os.getpgid(member.pid) == pgid and member.live():
                members.add(member)
        except (ProcessLookupError, psutil.NoSuchProcess):
            continue
        except (PermissionError, psutil.AccessDenied):
            unreadable.add(process.pid)
    return members, unreadable


def _owners_live(births: dict[str, Birth]) -> bool:
    return any(birth.owned().live() for birth in births.values())


def _closed_within(pgid: int, births: dict[str, Birth], bound: float) -> bool:
    deadline = time.monotonic() + bound
    while True:
        if not _owners_live(births) and group_empty(pgid, births.get("helper")):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POLL_S)


def _pinned(executor: Birth, pgid: int) -> bool:
    """The recorded executor, alive in the group, proves the group is still this job's."""
    owned = executor.owned()
    try:
        return owned.live() and os.getpgid(executor.pid) == pgid and owned.live()
    except ProcessLookupError:
        return False


def _kill(member: OwnedProcess) -> None:
    try:
        member.send_signal(signal.SIGKILL)
    except (PermissionError, psutil.AccessDenied):
        return


def _kill_pinned(executor: Birth, pgid: int) -> None:
    """SIGKILL exact births while the recorded executor still pins the group, itself last."""
    for _round in range(_KILL_ROUNDS):
        if not _pinned(executor, pgid):
            return
        members, _unreadable = group_members(pgid)
        others = {member for member in members if member.pid != executor.pid}
        if not others:
            break
        for member in others:
            _kill(member)
        time.sleep(_POLL_S)
    if _pinned(executor, pgid):
        _kill(executor.owned())


def _describe(member: OwnedProcess) -> str:
    try:
        name = psutil.Process(member.pid).name()
    except psutil.Error:
        name = "?"
    return f"pid {member.pid} ({name}, start {member.birth})"


def _refusal(pgid: int, births: dict[str, Birth]) -> str:
    members, unreadable = group_members(pgid)
    listed = [_describe(member) for member in sorted(members, key=lambda item: item.pid)]
    listed += [f"pid {pid} (unreadable)" for pid in sorted(unreadable)]
    alive = [name for name, birth in births.items() if birth.owned().live()]
    owners = f"; recorded {', '.join(alive)} still alive" if alive else ""
    shown = ", ".join(listed[:_EVIDENCE_MEMBERS]) or "none readable"
    return (
        f"terminal executor job still has live group members in process group {pgid} "
        f"({shown}{owners}); custody retained. launchd's job cleanup only sends SIGTERM "
        "to the group, and no recorded owner still pins it, so these processes cannot be "
        "proven to belong to this attempt and are not signalled. Confirm each is a "
        f"leftover of this release attempt (process group {pgid}), terminate it "
        "(kill -KILL <pid>), then retry the same update command."
    )


def prove_group_closed(pgid: int, births: dict[str, Birth] | None) -> None:
    """Recorded births closed and the job group empty, or refuse with evidence.

    Escalation is bounded and exact: only while the recorded executor is alive
    in the group (so its id cannot have been reused) are the group's captured
    members, then the executor, sent SIGKILL. Otherwise nothing is signalled.
    """
    owners = births or {}
    if _closed_within(pgid, owners, _CLOSURE_WAIT_S):
        return
    executor = owners.get("executor")
    if executor is not None and _pinned(executor, pgid):
        _kill_pinned(executor, pgid)
        if _closed_within(pgid, owners, _KILL_WAIT_S):
            return
    raise RuntimeError(_refusal(pgid, owners))
