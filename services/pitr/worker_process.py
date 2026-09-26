"""One directly owned worker process group per backup or PITR operation.

The controller launches a fixed worker module in a new session, retains the
unreaped direct child, and alone signals that group. Trusted tools inherit it.
A result is accepted only after confirmed group closure, a zero exit and the
caller's validation; every other outcome retains the private controls. Native
birth receipts identify processes; they never grant adoption or closure.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import types
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol, cast

import psutil

from shared.atomic_io import write_text_atomic
from shared.exec_process_domain import ExecDomainBirthError, ExecProcessDomain
from shared.native_process import native_boot_id
from shared.native_process.ownership import OwnedProcess
from shared.platform import file_lock

# A live worker gets this long to unwind its private cleanup (key files,
# decrypted scratch, a foreground sandbox) before the controller's confirmed close.
TERMINATE_GRACE_S = 3.0
CLOSE_DEADLINE_S = 20.0


class StopSignal(Protocol):
    def is_set(self) -> bool: ...

    def wait(self, timeout: float | None = None) -> bool: ...


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
class CompletedOperation:
    """A zero-exit result observed only after the launch-owned group closed.

    This is completion evidence, never authority to adopt or signal a process.
    Business callers validate their result and commit before retiring controls.
    """

    work: Path
    worker: NativeProcess
    result: dict[str, object]

    def retire(self) -> None:
        shutil.rmtree(self.work)


class OperationCustodyError(RuntimeError):
    """Keep the actual direct child and controls when closure is unresolved."""

    def __init__(self, work: Path, domain: ExecProcessDomain) -> None:
        super().__init__(f"operation group closure is unresolved; evidence: {work}")
        self.work = work
        self.domain = domain


def publish_result(path: Path, result: Mapping[str, object]) -> None:
    """Publish complete business output; the controller still owns closure."""
    write_text_atomic(
        path,
        json.dumps(result, sort_keys=True, separators=(",", ":")),
        mode=0o600,
        sync_parent=True,
    )


def _interrupt(signum: int, _frame: types.FrameType | None) -> None:
    # A repeated stop must not interrupt the worker's own cleanup halfway through.
    signal.signal(signum, signal.SIG_IGN)
    raise KeyboardInterrupt


def worker_request(argv: list[str]) -> tuple[dict[str, object], Path]:
    """Enter one fixed worker; SIGTERM unwinds its cleanup, never its closure.

    The request is complete before launch. The worker never signals its own
    group: its controller alone closes inherited descendants before acceptance.
    """
    signal.signal(signal.SIGTERM, _interrupt)
    if len(argv) != 3:
        raise SystemExit("usage: python -m <operation worker> REQUEST RESULT")
    value = json.loads(Path(argv[1]).read_text())
    if not isinstance(value, dict):
        raise TypeError("operation request must be an object")
    return cast(dict[str, object], value), Path(argv[2])


def _result_object(path: Path, work: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"operation result is missing or invalid; evidence: {work}") from exc
    if not isinstance(value, dict):
        raise TypeError(f"operation result must be an object; evidence: {work}")
    return cast(dict[str, object], value)


def _operation_tail(path: Path) -> str:
    with path.open("rb") as output:
        output.seek(max(0, path.stat().st_size - 4000))
        return output.read().decode(errors="replace")


async def _await_operation(
    domain: ExecProcessDomain, stop: StopSignal | None, deadline: float
) -> None:
    while domain.leader_alive():
        if stop is not None and stop.is_set():
            raise RuntimeError("operation was stopped")
        if time.monotonic() >= deadline:
            raise TimeoutError("operation exceeded its execution bound")
        await asyncio.sleep(0.05)


async def _request_stop(domain: ExecProcessDomain) -> None:
    """Let a live worker unwind its private cleanup; this never proves closure."""
    if not domain.leader_alive():
        return
    domain.signal(signal.SIGTERM)
    deadline = time.monotonic() + TERMINATE_GRACE_S
    while domain.leader_alive() and time.monotonic() < deadline:
        await asyncio.sleep(0.05)


def _close_operation(domain: ExecProcessDomain, work: Path) -> None:
    try:
        # No census authorizes release: signal the actual launch-owned group
        # while its direct child still pins the native number, then observe.
        domain.close_confirmed(time.monotonic() + CLOSE_DEADLINE_S)
    except BaseException as exc:
        raise OperationCustodyError(work, domain) from exc


async def _abort_operation(
    domain: ExecProcessDomain,
    process: subprocess.Popen[bytes],
    work: Path,
    original: BaseException,
) -> None:
    """Close an aborted operation; any closure doubt keeps the unreaped child.

    A cancellation that arrives during the cooperative grace still reaches the
    confirmed close, then propagates so the awaiting task stays cancelled.
    """
    failure = original
    try:
        await _request_stop(domain)
    except Exception as exc:  # The courtesy signal never replaces confirmed closure.
        original.add_note(f"cooperative stop failed: {exc!r}")
    except BaseException as exc:
        failure = exc
    try:
        _close_operation(domain, work)
        process.wait(timeout=1)
    except BaseException as cleanup:
        failure.add_note(f"operation cleanup failed: {cleanup}")
        raise failure from cleanup
    failure.add_note(f"operation controls retained: {work}")
    if failure is not original:
        raise failure from original


async def run_operation(
    module: str,
    request: Mapping[str, object],
    *,
    control_root: Path,
    env: dict[str, str],
    stop: StopSignal | None = None,
    timeout_s: float = 6 * 3600,
) -> CompletedOperation:
    """Run a fixed trusted worker; preserve controls on every failed outcome.

    The living controller alone owns cancellation: stop, timeout or task
    cancellation sends SIGTERM for bounded private cleanup, then closes the
    group. Retained controls refuse new work until explicitly retired. Persisted
    files cannot adopt this group after controller death; that requires a native
    executor boundary.
    """
    if os.name != "posix":
        raise RuntimeError("backup operation workers require POSIX")
    control_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with file_lock(control_root / ".lock", timeout_s=0):
        if any(path.name != ".lock" for path in control_root.iterdir()):
            raise RuntimeError(
                f"operation controls require explicit retirement before new work: {control_root}"
            )
        return await _run_owned_operation(module, request, control_root, env, stop, timeout_s)


async def _run_owned_operation(
    module: str,
    request: Mapping[str, object],
    control_root: Path,
    env: dict[str, str],
    stop: StopSignal | None,
    timeout_s: float,
) -> CompletedOperation:
    work = Path(tempfile.mkdtemp(prefix=".operation-", dir=control_root))
    source, result = work / "request.json", work / "result.json"
    publish_result(source, request)
    with (work / "stdout.log").open("xb") as stdout, (work / "stderr.log").open("xb") as stderr:
        os.fchmod(stdout.fileno(), 0o600)
        os.fchmod(stderr.fileno(), 0o600)
        try:
            process, domain = ExecProcessDomain.launch_posix(
                [sys.executable, "-I", "-B", "-m", module, str(source), str(result)],
                new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                env=env,
                cwd=Path(__file__).resolve().parents[2],
                close_fds=True,
            )
        except ExecDomainBirthError as exc:
            exc.add_note(f"operation controls retained: {work}")
            raise
        try:
            worker = NativeProcess.capture(psutil.Process(process.pid))
            await _await_operation(domain, stop, time.monotonic() + timeout_s)
        except BaseException as original:
            await _abort_operation(domain, process, work, original)
            raise
        _close_operation(domain, work)
        returncode = process.wait(timeout=1)
        if returncode != 0:
            raise RuntimeError(
                f"operation exited {returncode}; evidence: {work}: "
                f"{_operation_tail(work / 'stderr.log')}"
            )
        return CompletedOperation(work, worker, _result_object(result, work))
