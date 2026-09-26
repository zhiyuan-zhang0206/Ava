"""Custody records, quarantine and retirement for backup and PITR operations.

Each operation kind owns a private control root: a lock plus at most the
controls of operations whose custody is not settled. A control directory's
records decide what happens next:

- `closure.json` alone: the controller proved group closure and died before
  quarantine; the next admission finishes the quarantine.
- `committed.json`: the business commit happened; the controls just retire.
- `unresolved.json`, or no closure proof at all: custody is unproven. The kind
  is blocked until `ava pitr operations retire` re-proves closure.

Quarantine runs only after proven closure. The kind's sanitizer removes
plaintext database material and moves the worker's business receipts into the
controls, which then move under the kind's quarantine root with a failure
note. The newest entry always survives; older ones are pruned to a count and
byte bound. Unproven custody keeps the actual unreaped leader referenced for
the life of the controller, so it cannot be reaped and its number reused.
"""

from __future__ import annotations

import json
import logging
import math
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import psutil

from shared.atomic_io import write_text_atomic
from shared.exec_process_domain import (
    ExecProcessDomain,
    _darwin_group_listing,
    _process_group_has_live_member,
)
from shared.native_process import native_boot_id
from shared.native_process.ownership import OwnedProcess
from shared.platform import file_lock

_log = logging.getLogger(__name__)

# A live worker gets this long to unwind its private cleanup (key files,
# decrypted scratch, a foreground sandbox) before the controller's confirmed close.
TERMINATE_GRACE_S = 3.0
# A controller that still holds an unresolved leader re-attempts closure this
# long at each later admission of the same kind.
RETRY_CLOSE_DEADLINE_S = 5.0
# Quarantine bound per root: the newest entry always survives, then at most
# this many entries and bytes are kept.
QUARANTINE_KEEP = 10
QUARANTINE_MAX_BYTES = 8 * 1024**3


@dataclass(frozen=True)
class NativeProcess:
    """One recorded process in one boot; receipt equality remains byte-exact."""

    boot_id: str
    process: OwnedProcess

    @classmethod
    def capture(cls, process: psutil.Process) -> NativeProcess:
        boot = native_boot_id()
        if boot is None:
            raise RuntimeError("PITR process custody requires a native POSIX boot identity")
        return cls(boot, OwnedProcess.capture(process))

    @classmethod
    def from_value(cls, value: object) -> NativeProcess:
        if not isinstance(value, dict):
            raise TypeError("invalid PITR native process receipt")
        record = cast("dict[str, object]", value)
        if set(record) != {"boot_id", "process"}:
            raise RuntimeError("invalid PITR native process receipt")
        boot, raw_process = record["boot_id"], record["process"]
        if not isinstance(boot, str) or not boot or not isinstance(raw_process, dict):
            raise RuntimeError("invalid PITR native process receipt")
        process = cast("dict[str, object]", raw_process)
        return cls(boot, _parse_birth(process))

    def value(self) -> dict[str, object]:
        return asdict(self)

    def same_birth(self, other: NativeProcess) -> bool:
        return self.boot_id == other.boot_id and self.process.same_birth(other.process)

    def present(self) -> psutil.Process | None:
        """Include an unreaped zombie: it still pins the native PID/group number."""
        if self.boot_id != native_boot_id():
            raise RuntimeError("PITR process receipt belongs to another boot")
        try:
            current = psutil.Process(self.process.pid)
            if not self.process.same_birth(OwnedProcess.capture(current)):
                return None
            return current
        except psutil.NoSuchProcess:
            return None

    def live(self) -> psutil.Process | None:
        current = self.present()
        if current is None:
            return None
        try:
            return (
                current
                if current.status() not in {psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD}
                else None
            )
        except psutil.NoSuchProcess:
            return None


def _parse_birth(process: dict[str, object]) -> OwnedProcess:
    if set(process) != {"pid", "birth", "starttime"}:
        raise RuntimeError("incomplete PITR native process birth")
    pid, birth, ticks = process["pid"], process["birth"], process["starttime"]
    if type(pid) is not int or pid <= 0:
        raise RuntimeError("invalid PITR native PID")
    if (
        isinstance(birth, bool)
        or not isinstance(birth, (int, float))
        or not math.isfinite(birth)
        or birth <= 0
    ):
        raise RuntimeError("invalid PITR native process birth")
    if ticks is not None and (type(ticks) is not int or ticks < 0):
        raise RuntimeError("invalid PITR native start ticks")
    identity = OwnedProcess(pid, float(birth), ticks)
    identity.birth_key()
    return identity


@dataclass(frozen=True)
class OperationWorker:
    """The launched worker as its controller recorded it."""

    pid: int
    native: NativeProcess | None


def claims_receipt(value: object, worker: OperationWorker | None) -> bool:
    """Whether a business receipt belongs to a closed worker of this operation.

    A receipt from an earlier boot is closed by the reboot. Otherwise it must
    name the recorded worker: its exact birth, or its PID in this boot when
    the birth capture itself failed.
    """
    recorded = NativeProcess.from_value(value)
    if recorded.boot_id != native_boot_id():
        return True
    if worker is None:
        return False
    if worker.native is not None:
        return worker.native.same_birth(recorded)
    return recorded.process.pid == worker.pid


def no_business_staging(_work: Path, _worker: OperationWorker | None) -> None:
    """The kind keeps all of its staging inside the operation's controls."""


@dataclass(frozen=True)
class OperationKind:
    """One operation kind's private controls, quarantine and sanitizer.

    `sanitize(work, worker)` runs only after group closure is proven. It must
    remove the kind's plaintext database material and move the worker's
    business receipts under `work`, so the quarantined controls hold evidence
    and the next operation of the kind can start.
    """

    name: str
    control_root: Path
    quarantine_root: Path
    sanitize: Callable[[Path, OperationWorker | None], None] = no_business_staging
    grace_s: float = TERMINATE_GRACE_S


class OperationDeferred(RuntimeError):  # noqa: N818 -- a clean outcome, not an error
    """The worker declined before creating evidence; its controls retired."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"operation deferred ({reason}): {detail}")
        self.reason = reason
        self.detail = detail


class OperationBlockedError(RuntimeError):
    """Unproven custody of an earlier operation refuses new work of its kind."""

    def __init__(self, kind: OperationKind, blocked: list[tuple[Path, str]]) -> None:
        listing = "; ".join(f"{path.name}: {reason}" for path, reason in blocked)
        super().__init__(
            f"{kind.name} operations are blocked until `ava pitr operations retire` "
            f"proves closure ({kind.control_root}): {listing}"
        )
        self.blocked = blocked


class OperationCustodyError(RuntimeError):
    """Group closure is unresolved; the controller keeps the unreaped leader."""

    def __init__(self, work: Path) -> None:
        super().__init__(
            f"operation group closure is unresolved; its controls stay and block: {work}"
        )
        self.work = work


def _now() -> str:
    return datetime.now(UTC).isoformat()


def publish_result(path: Path, result: Mapping[str, object]) -> None:
    """Publish one complete private record; the controller still owns closure."""
    write_text_atomic(
        path,
        json.dumps(result, sort_keys=True, separators=(",", ":")),
        mode=0o600,
        sync_parent=True,
    )


def record_closure(work: Path, proven_by: str, returncode: int | None) -> None:
    """Receipt of proven closure; a missing one only means asking for retirement."""
    with suppress(OSError):
        publish_result(
            work / "closure.json",
            {"proven_by": proven_by, "at": _now(), "returncode": returncode},
        )


def recorded_worker(work: Path) -> OperationWorker | None:
    """The worker the controller recorded, or None when it died while launching."""
    try:
        value = json.loads((work / "worker.json").read_text())
    except FileNotFoundError:
        return None
    native = value["native"]
    return OperationWorker(
        int(value["pid"]), None if native is None else NativeProcess.from_value(native)
    )


@dataclass(frozen=True)
class _Held:
    kind: str
    work: Path
    process: subprocess.Popen[bytes]
    domain: ExecProcessDomain | None


# Unresolved leaders stay referenced for the life of the controller process, so
# neither `Popen.__del__` nor `subprocess._cleanup` can reap and release them.
_HELD: list[_Held] = []
_HELD_LOCK = threading.Lock()


def is_stop(exc: BaseException) -> bool:
    """Cancellation, KeyboardInterrupt and SystemExit propagate as stops."""
    return not isinstance(exc, Exception)


def hold(
    kind: OperationKind,
    work: Path,
    process: subprocess.Popen[bytes],
    domain: ExecProcessDomain | None,
    cleanup: BaseException,
    failure: BaseException | None,
) -> BaseException:
    """Keep the unreaped leader and blocking controls; return what to raise.

    A stop still propagates as the stop, carrying the custody error as its
    cause; any other failure becomes `OperationCustodyError`.
    """
    with _HELD_LOCK:
        _HELD.append(_Held(kind.name, work, process, domain))
    with suppress(OSError):
        publish_result(work / "unresolved.json", {"at": _now(), "error": repr(cleanup)[:4000]})
    report(kind, "blocked", f"closure unresolved for {work.name}: {cleanup!r}")
    custody = OperationCustodyError(work)
    custody.add_note(f"closure failure: {cleanup!r}")
    if failure is not None and is_stop(failure):
        failure.add_note(str(custody))
        failure.__cause__ = custody
        return failure
    custody.__cause__ = failure if failure is not None else cleanup
    return custody


def held_operations() -> list[Path]:
    """Controls whose unresolved leader this controller process still holds."""
    with _HELD_LOCK:
        return [item.work for item in _HELD]


def close_unowned_launch(process: subprocess.Popen[bytes], deadline: float) -> None:
    """Confirm closure of a group whose exec domain was never admitted.

    The unreaped direct child pins its PID and group number, so signalling
    that group reaches only this launch. Closure is confirmed as the exec
    domain confirms it: no live member, and on macOS a kernel listing of the
    exited leader alone.
    """
    if process.returncode is not None:
        raise RuntimeError("the launched leader was already reaped")
    group = process.pid
    while True:
        try:
            os.killpg(group, signal.SIGKILL)
        except PermissionError:
            # XNU returns EPERM for a group whose members are all zombies.
            if sys.platform != "darwin":
                raise
        if not _process_group_has_live_member(group) and _only_leader_listed(group, deadline):
            return
        if time.monotonic() >= deadline:
            raise TimeoutError("the launched group still has live members")
        time.sleep(0.05)


def _only_leader_listed(group: int, deadline: float) -> bool:
    if sys.platform != "darwin":
        return True
    # Observe the signalled leader's exit without reaping it, so no fork is in
    # flight, then read the kernel's atomic group listing.
    while _leader_status(group) not in {psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD}:
        if time.monotonic() >= deadline:
            raise TimeoutError("the launched leader is still live after its group signal")
        time.sleep(0.05)
    return _darwin_group_listing(group) == [group]


def _leader_status(pid: int) -> str:
    try:
        return psutil.Process(pid).status()
    except psutil.NoSuchProcess:
        return psutil.STATUS_DEAD


def _retry_held(kind: OperationKind) -> None:
    """Re-attempt closure of this kind's held leaders; release stays explicit."""
    with _HELD_LOCK:
        held = [item for item in _HELD if item.kind == kind.name]
    for item in held:
        try:
            deadline = time.monotonic() + RETRY_CLOSE_DEADLINE_S
            if item.domain is not None:
                item.domain.close_confirmed(deadline)
            else:
                close_unowned_launch(item.process, deadline)
            returncode = item.process.wait(timeout=1)
        except Exception as exc:
            _log.warning("[backup-operation] %s closure is still unresolved: %r", kind.name, exc)
            continue
        with _HELD_LOCK:
            _HELD.remove(item)
        if item.work.is_dir():
            record_closure(item.work, "controller-retry", returncode)


def retire_controls(work: Path) -> None:
    """Retire controls atomically first, so a crash cannot leave a torn record."""
    retired = work.with_name(".retired-" + work.name.removeprefix(".operation-"))
    work.rename(retired)
    shutil.rmtree(retired)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def sweep_closed_partials(directory: Path) -> None:
    """Remove intermediates whose writers closed; the caller excludes new writers.

    Only a process holding a partial open can still extend it, so a partial no
    process holds open has a closed writer and is removed. One still open (a
    killed run's orphaned tool) stays, is reported, and goes on a later run.
    """
    stale = [
        path
        for path in directory.iterdir()
        if path.is_file()
        and (path.name.endswith(".partial") or path.name.startswith(".backup-key-"))
    ]
    if not stale:
        return
    held = _held_open({str(path.resolve()) for path in stale})
    for path in stale:
        if str(path.resolve()) in held:
            _log.error("[backup] %s is still held open by a live writer; kept for now", path.name)
        else:
            path.unlink(missing_ok=True)


def _held_open(paths: set[str]) -> set[str]:
    held: set[str] = set()
    for process in psutil.process_iter():
        with suppress(psutil.Error):
            held.update(item.path for item in process.open_files() if item.path in paths)
    return held


def quarantine(
    kind: OperationKind, work: Path, failure: str, *, custody: str = "quarantined"
) -> Path:
    """Sanitize proven-closed controls and move them into the kind's quarantine."""
    kind.sanitize(work, recorded_worker(work))
    write_text_atomic(work / "failure.txt", failure, mode=0o600)
    kind.quarantine_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    entry = kind.quarantine_root / f"{stamp}-{kind.name}-{work.name.removeprefix('.operation-')}"
    work.rename(entry)
    _fsync_dir(work.parent)
    _fsync_dir(entry.parent)
    _prune_quarantine(kind.quarantine_root, entry)
    report(kind, custody, f"{entry.name}: {failure.splitlines()[0] if failure else ''}")
    return entry


def _tree_bytes(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            with suppress(OSError):
                total += (Path(root) / name).lstat().st_size
    return total


def quarantine_entries(root: Path) -> list[Path]:
    """Quarantined operations, oldest first (entry names start with a UTC stamp)."""
    if not root.is_dir():
        return []
    return sorted(
        path
        for path in root.iterdir()
        if path.is_dir() and not path.is_symlink() and not path.name.startswith(".")
    )


def _prune_quarantine(root: Path, newest: Path) -> None:
    entries = quarantine_entries(root)
    sizes = {entry: _tree_bytes(entry) for entry in entries}
    count, total = len(entries), sum(sizes.values())
    for entry in entries:
        if count <= QUARANTINE_KEEP and total <= QUARANTINE_MAX_BYTES:
            return
        if entry == newest:
            continue
        shutil.rmtree(entry)
        count, total = count - 1, total - sizes[entry]


def report(kind: OperationKind, custody: str, detail: str) -> None:
    """Log and alert one custody transition; a retirement is informational."""
    from shared import telemetry

    bounded = detail[:2000]
    retired = custody == "retired"
    _log.log(
        logging.INFO if retired else logging.ERROR,
        "[backup-operation] %s %s: %s",
        kind.name,
        custody,
        bounded,
    )
    telemetry.emit(
        "telemetry",
        "backup_operation_custody",
        level="info" if retired else "error",
        attributes={"operation": kind.name, "custody": custody, "detail": bounded},
    )


def _custody_state(work: Path) -> tuple[str, str]:
    """(`committed` | `closed` | `blocked`, reason) for one control directory."""
    if (work / "unresolved.json").exists():
        later = (work / "closure.json").exists()
        return "blocked", "closure was unresolved at failure" + (
            "; its controller confirmed it later" if later else ""
        )
    if (work / "committed.json").exists():
        return "committed", "business commit completed"
    if (work / "closure.json").exists():
        return "closed", "closure proven before the controller stopped"
    return "blocked", "the controller stopped before proving closure"


def _operation_dirs(root: Path) -> list[tuple[Path, str, str]]:
    entries: list[tuple[Path, str, str]] = []
    if not root.is_dir():
        return entries
    for path in sorted(root.iterdir()):
        if path.name == ".lock":
            continue
        if path.is_symlink() or not path.is_dir():
            entries.append((path, "blocked", "unexpected entry in the control root"))
        elif path.name.startswith(".retired-"):
            entries.append((path, "retired", "retirement was interrupted"))
        elif path.name.startswith(".operation-"):
            entries.append((path, *_custody_state(path)))
        else:
            entries.append((path, "blocked", "unexpected entry in the control root"))
    return entries


def blocked_operations(kind: OperationKind) -> list[tuple[Path, str]]:
    """Control directories that refuse new work of this kind, with reasons."""
    return [
        (path, reason)
        for path, state, reason in _operation_dirs(kind.control_root)
        if state == "blocked"
    ]


def admit(kind: OperationKind) -> None:
    """Finish proven leftovers of this kind; refuse while any custody is unproven."""
    _retry_held(kind)
    blocked: list[tuple[Path, str]] = []
    for path, state, reason in _operation_dirs(kind.control_root):
        if state == "retired":
            shutil.rmtree(path)
        elif state == "committed":
            retire_controls(path)
        elif state == "closed":
            try:
                quarantine(kind, path, f"controller stopped after proving closure ({reason})")
            except Exception as exc:
                blocked.append((path, f"quarantine failed: {exc!r}"))
        else:
            blocked.append((path, reason))
    if blocked:
        error = OperationBlockedError(kind, blocked)
        report(kind, "blocked", str(error))
        raise error


def prove_closure(work: Path) -> tuple[bool, str]:
    """Re-prove group closure without the original controller, or say why not.

    The group number stays reserved while any member exists, so an empty
    group, or a different process born under the recorded worker PID, proves
    that every inherited member has exited. A process that called setsid
    escaped the group and is outside this proof, as it is outside the
    controller's own closure.
    """
    if (work / "closure.json").exists():
        return True, "its controller confirmed group closure"
    try:
        operation = json.loads((work / "operation.json").read_text())
    except FileNotFoundError:
        return False, "no operation record exists"
    if operation["boot_id"] != native_boot_id():
        return True, "the host rebooted after the operation launched"
    worker = recorded_worker(work)
    if worker is None:
        return False, "the controller stopped while launching; only a reboot proves closure"
    if worker.native is not None and worker.native.present() is not None:
        return False, f"worker {worker.pid} is still present (running or held by its controller)"
    if worker.native is None and psutil.pid_exists(worker.pid):
        return False, f"PID {worker.pid} exists and its birth was never recorded"
    try:
        os.killpg(worker.pid, 0)
    except ProcessLookupError:
        return True, f"process group {worker.pid} is empty"
    except PermissionError:
        return False, f"process group {worker.pid} still has members"
    if worker.native is not None and psutil.pid_exists(worker.pid):
        # The kernel reuses the worker's number only after its group emptied.
        return True, f"PID {worker.pid} now names a later process; the group had emptied"
    return False, f"process group {worker.pid} still has members"


@dataclass(frozen=True)
class RetireReport:
    work: Path
    proven: bool
    reason: str
    entry: Path | None


def retire_blocked(kind: OperationKind, *, confirm: bool) -> list[RetireReport]:
    """Re-prove closure of each blocked operation; with `confirm`, quarantine it.

    Raises `LockTimeoutError` while an operation of the kind is running.
    """
    if not kind.control_root.is_dir():
        return []
    reports: list[RetireReport] = []
    with file_lock(kind.control_root / ".lock", timeout_s=0):
        for work, _reason in blocked_operations(kind):
            if not work.name.startswith(".operation-") or work.is_symlink():
                reports.append(
                    RetireReport(work, proven=False, reason="not an operation", entry=None)
                )
                continue
            proven, reason = prove_closure(work)
            entry = None
            if proven and confirm:
                if not (work / "closure.json").exists():
                    record_closure(work, "retire", None)
                entry = quarantine(
                    kind,
                    work,
                    f"retired by operator after closure proof: {reason}",
                    custody="retired",
                )
            reports.append(RetireReport(work, proven, reason, entry))
    return reports
