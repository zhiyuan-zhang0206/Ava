"""One directly owned worker process group per backup or PITR operation.

The controller launches a fixed worker module in a new session, retains the
unreaped direct child, and alone signals that group. Trusted tools inherit it.
A bootstrap puts the controller's own code root first on the worker's path and
refuses any other import origin. Secrets reach the worker on stdin, never in a
retained control file.

Every outcome settles custody (`services.pitr.operation_custody`):

- **accepted**: confirmed group closure, a zero exit, a valid result and the
  caller's commit; the controls retire.
- **deferred**: the worker declined before creating evidence (a busy lock,
  missing space); the controls retire and the caller reschedules.
- **quarantined**: any other outcome whose group closure the controller
  proved, stop and drain cancellation included; an alert is raised and the
  next operation of the kind proceeds.
- **blocked**: closure is unproven; the kind refuses new work and alerts until
  `ava pitr operations retire` re-proves closure.

Custody steps (launch, closure, commit, quarantine) run to completion off the
event loop. A stop that arrives meanwhile waits for the step's real outcome,
then propagates as a stop; it is never absorbed into an ordinary error.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import types
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast

import psutil

from services.pitr.operation_custody import (
    NativeProcess,
    OperationDeferred,
    OperationKind,
    admit,
    close_unowned_launch,
    hold,
    is_stop,
    publish_result,
    quarantine,
    record_closure,
    report,
    retire_controls,
)
from shared.exec_process_domain import ExecDomainBirthError, ExecProcessDomain
from shared.native_process import native_boot_id
from shared.platform import file_lock

CLOSE_DEADLINE_S = 20.0
_FAILURE_TEXT_LIMIT = 16_000
_CODE_ROOT = Path(__file__).resolve().parents[2]
# Runs before any worker import: the controller's checkout, not whatever the
# interpreter's editable install names, supplies every module the worker runs.
_BOOTSTRAP = """\
import importlib.util, pathlib, runpy, sys
root, module = pathlib.Path(sys.argv[1]), sys.argv[2]
sys.path.insert(0, str(root))
for name in ("shared", "services.pitr", module):
    spec = importlib.util.find_spec(name)
    origin = None if spec is None else spec.origin
    if origin is None or not pathlib.Path(origin).resolve().is_relative_to(root):
        raise SystemExit(f"operation worker code root mismatch: {name} resolves to {origin}")
sys.argv = [module, *sys.argv[3:]]
runpy.run_module(module, run_name="__main__", alter_sys=True)
"""


class StopSignal(Protocol):
    def is_set(self) -> bool: ...

    def wait(self, timeout: float | None = None) -> bool: ...


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


def worker_secrets() -> dict[str, str]:
    """Read the controller's secret mapping from stdin; it never reaches disk."""
    value: object = json.loads(sys.stdin.buffer.read() or b"{}")
    if not isinstance(value, dict):
        raise TypeError("operation secrets must be an object")
    secrets = cast(dict[object, object], value)
    if not all(isinstance(key, str) and isinstance(item, str) for key, item in secrets.items()):
        raise TypeError("operation secrets must map names to strings")
    return cast(dict[str, str], secrets)


def _result_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise RuntimeError("operation result is missing or invalid") from exc
    if not isinstance(value, dict):
        raise TypeError("operation result must be an object")
    return cast(dict[str, object], value)


def _deferral(result: dict[str, object]) -> OperationDeferred | None:
    if set(result) != {"deferred", "detail"}:
        return None
    reason, detail = result["deferred"], result["detail"]
    if not isinstance(reason, str) or not isinstance(detail, str):
        raise TypeError("operation deferral must name its reason and detail")
    return OperationDeferred(reason, detail)


def _operation_tail(path: Path) -> str:
    with path.open("rb") as output:
        output.seek(max(0, path.stat().st_size - 4000))
        return output.read().decode(errors="replace")


class _Tail:
    """Forward the worker's complete stderr lines to an operator sink."""

    def __init__(self, path: Path, sink: Callable[[str], None]) -> None:
        self._path = path
        self._sink = sink
        self._offset = 0
        self._pending = b""

    def pump(self) -> None:
        with self._path.open("rb") as log:
            log.seek(self._offset)
            chunk = log.read()
        self._offset += len(chunk)
        *lines, self._pending = (self._pending + chunk).split(b"\n")
        for line in lines:
            self._sink(line.decode(errors="replace"))

    def flush(self) -> None:
        self.pump()
        if self._pending:
            self._sink(self._pending.decode(errors="replace"))
            self._pending = b""


async def _to_completion[T](
    step: Callable[[], T],
) -> tuple[asyncio.Future[T], BaseException | None]:
    """Finish one custody step off the event loop, even when this task is stopped.

    A cancellation or KeyboardInterrupt that arrives meanwhile is returned, not
    raised: the caller settles custody with the step's real outcome and then
    propagates the stop. The loop and its health server stay responsive.
    """
    future = asyncio.get_running_loop().run_in_executor(None, step)
    stopped: BaseException | None = None
    while not future.done():
        try:
            await asyncio.wait({future})
        except GeneratorExit:
            raise
        except BaseException as exc:  # A stop waits for the step it interrupted.
            stopped = stopped or exc
    return future, stopped


def _complete[T](future: asyncio.Future[T], stopped: BaseException | None) -> T:
    """The step's value; a deferred stop still wins over it and its failure."""
    if stopped is None:
        return future.result()
    failure = future.exception()
    if failure is not None:
        stopped.add_note(f"interrupted custody step failed: {failure!r}")
    raise stopped


async def run_operation(
    module: str,
    request: Mapping[str, object],
    *,
    kind: OperationKind,
    env: dict[str, str],
    secrets: Mapping[str, str] | None = None,
    stop: StopSignal | None = None,
    timeout_s: float = 6 * 3600,
    progress: Callable[[str], None] | None = None,
) -> CompletedOperation:
    """Run a fixed trusted worker and settle its custody on every outcome.

    The living controller alone owns cancellation: stop, timeout or task
    cancellation sends SIGTERM for the kind's grace, then closes the group.
    Proven closure quarantines a failed operation; unproven closure blocks the
    kind. `progress` receives the worker's stderr lines as they are written.
    Persisted files cannot adopt this group after controller death: that
    needs `ava pitr operations retire` to re-prove closure first.
    """
    if os.name != "posix":
        raise RuntimeError("backup operation workers require POSIX")
    for root in (kind.control_root, kind.quarantine_root):
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with file_lock(kind.control_root / ".lock", timeout_s=0):
        _complete(*await _to_completion(lambda: admit(kind)))
        return await _run_owned(module, request, kind, env, secrets, stop, timeout_s, progress)


async def _run_owned(
    module: str,
    request: Mapping[str, object],
    kind: OperationKind,
    env: dict[str, str],
    secrets: Mapping[str, str] | None,
    stop: StopSignal | None,
    timeout_s: float,
    progress: Callable[[str], None] | None,
) -> CompletedOperation:
    work = Path(tempfile.mkdtemp(prefix=".operation-", dir=kind.control_root))
    try:
        publish_result(
            work / "operation.json",
            {"kind": kind.name, "module": module, "boot_id": native_boot_id(), "at": _now()},
        )
        publish_result(work / "request.json", request)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)  # Nothing was launched.
        raise
    tail = None if progress is None else _Tail(work / "stderr.log", progress)
    future, stopped = await _to_completion(lambda: _launch(module, work, env, secrets))
    launch_error = future.exception()
    if isinstance(launch_error, ExecDomainBirthError):
        raise await _settle_unowned_birth(kind, work, launch_error, stopped)
    if launch_error is not None:
        # Popen raised before creating a child, or reaped its failed exec.
        record_closure(work, "no-process", None)
        raise await _quarantined(kind, work, launch_error, stopped)
    process, domain = future.result()
    if stopped is not None:
        with suppress(OSError):  # Lets a later retirement name the group.
            publish_result(work / "worker.json", {"pid": process.pid, "native": None})
        raise await _abort(kind, work, process, domain, stopped, tail)
    try:
        worker = _record_worker(work, process)
        await _await_operation(domain, stop, time.monotonic() + timeout_s, tail)
    except BaseException as original:
        aborted = original
    else:
        return await _accept(kind, work, process, domain, worker, tail)
    raise await _abort(kind, work, process, domain, aborted, tail)


def _record_worker(work: Path, process: subprocess.Popen[bytes]) -> NativeProcess:
    """Record the leader's PID at once, then its native birth for later proofs."""
    publish_result(work / "worker.json", {"pid": process.pid, "native": None})
    worker = NativeProcess.capture(psutil.Process(process.pid))
    publish_result(work / "worker.json", {"pid": process.pid, "native": worker.value()})
    return worker


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _launch(
    module: str, work: Path, env: dict[str, str], secrets: Mapping[str, str] | None
) -> tuple[subprocess.Popen[bytes], ExecProcessDomain]:
    with (work / "stdout.log").open("xb") as stdout, (work / "stderr.log").open("xb") as stderr:
        os.fchmod(stdout.fileno(), 0o600)
        os.fchmod(stderr.fileno(), 0o600)
        process, domain = ExecProcessDomain.launch_posix(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                _BOOTSTRAP,
                str(_CODE_ROOT),
                module,
                str(work / "request.json"),
                str(work / "result.json"),
            ],
            new_session=True,
            stdin=subprocess.DEVNULL if secrets is None else subprocess.PIPE,
            stdout=stdout,
            stderr=stderr,
            env=env,
            cwd=_CODE_ROOT,
            close_fds=True,
        )
    if secrets is not None and process.stdin is not None:
        # Never raises after launch: the handle must reach custody. A worker
        # that cannot read its secrets fails on its own and is closed normally.
        with suppress(OSError):
            process.stdin.write(json.dumps(dict(secrets)).encode())
        with suppress(OSError):
            process.stdin.close()
    return process, domain


async def _await_operation(
    domain: ExecProcessDomain, stop: StopSignal | None, deadline: float, tail: _Tail | None
) -> None:
    while domain.leader_alive():
        if tail is not None:
            tail.pump()
        if stop is not None and stop.is_set():
            raise RuntimeError("operation was stopped")
        if time.monotonic() >= deadline:
            raise TimeoutError("operation exceeded its execution bound")
        await asyncio.sleep(0.05)


async def _request_stop(domain: ExecProcessDomain, grace_s: float) -> None:
    """Let a live worker unwind its private cleanup; this never proves closure."""
    if not domain.leader_alive():
        return
    domain.signal(signal.SIGTERM)
    deadline = time.monotonic() + grace_s
    while domain.leader_alive() and time.monotonic() < deadline:
        await asyncio.sleep(0.05)


def _close_and_reap(domain: ExecProcessDomain, process: subprocess.Popen[bytes]) -> int:
    # No census authorizes release: signal the actual launch-owned group while
    # its direct child still pins the native number, then observe.
    domain.close_confirmed(time.monotonic() + CLOSE_DEADLINE_S)
    return process.wait(timeout=1)


async def _abort(
    kind: OperationKind,
    work: Path,
    process: subprocess.Popen[bytes],
    domain: ExecProcessDomain,
    original: BaseException,
    tail: _Tail | None,
) -> BaseException:
    """Close an aborted operation, then quarantine it or block its kind.

    A stop that arrives during the cooperative grace or the close still reaches
    the confirmed close, then propagates so the awaiting task stays stopped.
    Returns the exception the caller raises.
    """
    failure = original
    try:
        await _request_stop(domain, kind.grace_s)
    except Exception as exc:  # The courtesy signal never replaces confirmed closure.
        original.add_note(f"cooperative stop failed: {exc!r}")
    except BaseException as exc:
        failure = exc
    future, stopped = await _to_completion(lambda: _close_and_reap(domain, process))
    if stopped is not None and not is_stop(failure):
        failure = stopped
    cleanup = future.exception()
    if cleanup is not None:
        return hold(kind, work, process, domain, cleanup, failure)
    record_closure(work, "controller", future.result())
    if tail is not None:
        tail.flush()
    return await _quarantined(kind, work, failure)


async def _accept(
    kind: OperationKind,
    work: Path,
    process: subprocess.Popen[bytes],
    domain: ExecProcessDomain,
    worker: NativeProcess,
    tail: _Tail | None,
) -> CompletedOperation:
    future, stopped = await _to_completion(lambda: _close_and_reap(domain, process))
    cleanup = future.exception()
    if cleanup is not None:
        raise hold(kind, work, process, domain, cleanup, stopped)
    returncode = future.result()
    record_closure(work, "controller", returncode)
    if tail is not None:
        tail.flush()
    if stopped is not None:
        raise await _quarantined(kind, work, stopped)
    if returncode != 0:
        tail_text = _operation_tail(work / "stderr.log")
        raise await _quarantined(
            kind, work, RuntimeError(f"operation exited {returncode}: {tail_text}")
        )
    try:
        result = _result_object(work / "result.json")
        deferred = _deferral(result)
    except (RuntimeError, TypeError) as exc:
        invalid = exc
    else:
        if deferred is None:
            return CompletedOperation(kind, work, worker, result)
        _complete(*await _to_completion(lambda: retire_controls(work)))
        raise deferred
    raise await _quarantined(kind, work, invalid)


async def _settle_unowned_birth(
    kind: OperationKind,
    work: Path,
    error: ExecDomainBirthError,
    stopped: BaseException | None,
) -> BaseException:
    """Close a launch whose native birth capture failed, through its pinned leader."""
    process = error.proc
    with suppress(OSError):
        publish_result(work / "worker.json", {"pid": process.pid, "native": None})
    if process.stdin is not None:
        with suppress(OSError):
            process.stdin.close()

    def close() -> int:
        close_unowned_launch(process, time.monotonic() + CLOSE_DEADLINE_S)
        return process.wait(timeout=1)

    future, more = await _to_completion(close)
    stopped = stopped or more
    cleanup = future.exception()
    if cleanup is not None:
        return hold(kind, work, process, None, cleanup, stopped or error)
    record_closure(work, "controller", future.result())
    error.add_note("the launched group was closed after its birth capture failed")
    return await _quarantined(kind, work, error, stopped)


async def _quarantined(
    kind: OperationKind,
    work: Path,
    failure: BaseException,
    stopped: BaseException | None = None,
) -> BaseException:
    """Quarantine controls whose group closure is proven; return what to raise."""
    future, more = await _to_completion(lambda: quarantine(kind, work, _describe(failure)))
    stopped = stopped or more
    problem = future.exception()
    if problem is None:
        failure.add_note(f"operation quarantined: {future.result()}")
    else:
        failure.add_note(f"operation quarantine failed; its controls remain at {work}: {problem!r}")
        report(kind, "blocked", f"quarantine failed for {work.name}: {problem!r}")
    if stopped is not None and not is_stop(failure):
        stopped.__cause__ = failure
        return stopped
    return failure


def _describe(failure: BaseException) -> str:
    notes = "\n".join(getattr(failure, "__notes__", ()))
    return f"{type(failure).__name__}: {failure}\n{notes}"[:_FAILURE_TEXT_LIMIT]


@dataclass(frozen=True)
class CompletedOperation:
    """A zero-exit result observed only after the launch-owned group closed.

    This is completion evidence, never authority to adopt or signal a process.
    `commit` validates and publishes the result, then retires the controls.
    """

    kind: OperationKind
    work: Path
    worker: NativeProcess
    result: dict[str, object]

    async def commit[T](self, accept: Callable[[], T]) -> T:
        """Validate and commit off the event loop; success retires the controls.

        Any failure quarantines the controls: the group is already closed. A
        stop that arrives meanwhile waits for the commit, then propagates.
        """

        def committed() -> T:
            value = accept()
            publish_result(self.work / "committed.json", {"at": _now()})
            retire_controls(self.work)
            return value

        future, stopped = await _to_completion(committed)
        failure = future.exception()
        if failure is None:
            if stopped is not None:
                raise stopped
            return future.result()
        if (self.work / "committed.json").exists():
            failure.add_note(f"business commit completed; controls retire later: {self.work}")
            raise stopped or failure
        raise await _quarantined(self.kind, self.work, failure, stopped)
