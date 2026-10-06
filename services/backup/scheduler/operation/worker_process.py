"""Run a trusted backup worker with bounded stop and private artifact staging."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
import types
from collections.abc import Callable, Mapping
from contextlib import ExitStack, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, cast

from base.native_process.os_platform import LockTimeoutError, file_lock
from services.backup.scheduler.operation.staging import (
    OperationBusyError,
    OperationDeferred,
    OperationKind,
    cleanup,
    publish_result,
)

_log = logging.getLogger(__name__)
_CODE_ROOT = Path(__file__).resolve().parents[4]
_BOOTSTRAP = """\
import importlib.util, pathlib, runpy, sys
root, module = pathlib.Path(sys.argv[1]), sys.argv[2]
sys.path.insert(0, str(root))
for name in ("base", "services.backup.scheduler.operation", module):
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
    """Enter one fixed worker; SIGTERM unwinds its normal cleanup.

    The request is complete before launch. The worker never signals its own
    group: its controller owns bounded stop signaling.
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
    """Forward the worker's complete stderr lines to an operator sink.

    Progress is a courtesy: a sink that fails (an operator's closed stderr
    pipe) is dropped and never fails, cancels or loses the operation.
    """

    def __init__(self, path: Path, sink: Callable[[str], None]) -> None:
        self._path = path
        self._sink: Callable[[str], None] | None = sink
        self._offset = 0
        self._pending = b""

    def _emit(self, line: bytes) -> None:
        if self._sink is None:
            return
        try:
            self._sink(line.decode(errors="replace"))
        except Exception as exc:
            _log.warning("[backup-operation] progress sink dropped: %r", exc)
            self._sink = None

    def pump(self) -> None:
        if self._sink is None:
            return
        with self._path.open("rb") as log:
            log.seek(self._offset)
            chunk = log.read()
        self._offset += len(chunk)
        *lines, self._pending = (self._pending + chunk).split(b"\n")
        for line in lines:
            self._emit(line)

    def flush(self) -> None:
        self.pump()
        if self._pending:
            self._emit(self._pending)
            self._pending = b""


async def _to_completion[T](
    step: Callable[[], T],
) -> tuple[asyncio.Future[T], BaseException | None]:
    """Finish one launch, cleanup or commit step off the event loop, even when this task is stopped.

    A cancellation or KeyboardInterrupt that arrives meanwhile is returned, not
    raised: the caller receives the step's real outcome and then
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
        stopped.add_note(f"interrupted worker step failed: {failure!r}")
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
    """Run a trusted worker; accept only zero exit and a valid result object.

    Each operation uses independent private scratch. Cancellation asks the
    worker to unwind, then bounds termination of its known child/group. There
    is no durable custody, orphan census, quarantine or blocked-kind retirement.
    """
    if os.name != "posix":
        raise RuntimeError("backup operation workers require POSIX")
    kind.control_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    held = ExitStack()
    try:
        held.enter_context(file_lock(kind.control_root / ".lock", timeout_s=0))
    except LockTimeoutError as exc:
        held.close()
        raise OperationBusyError(f"{kind.name} operation is already running") from exc
    try:
        work = Path(tempfile.mkdtemp(prefix=".operation-", dir=kind.control_root))
    except BaseException:
        held.close()
        raise
    process: subprocess.Popen[bytes] | None = None
    try:
        publish_result(work / "request.json", request)
        future, cancelled = await _to_completion(lambda: _launch(module, work, env, secrets))
        process = future.result()
        _raise_stopped(cancelled)
        tail = None if progress is None else _Tail(work / "stderr.log", progress)
        await _await_operation(process, stop, time.monotonic() + timeout_s, tail)
        if tail is not None:
            tail.flush()
        result = _finished_result(process, work)
        return CompletedOperation(kind, work, result, held)
    except BaseException as original:
        try:
            if process is not None:
                (await _to_completion(lambda: _stop_worker(process, kind.grace_s)))[0].result()
            (await _to_completion(lambda: cleanup(kind, work)))[0].result()
        except BaseException as failure:
            original.add_note(f"operation cleanup failed at {work}: {failure!r}")
            _log.error("backup staging cleanup failed at %s: %r", work, failure)
        held.close()
        raise


def _raise_stopped(stopped: BaseException | None) -> None:
    if stopped is not None:
        raise stopped


def _finished_result(process: subprocess.Popen[bytes], work: Path) -> dict[str, object]:
    if process.returncode != 0:
        raise RuntimeError(
            f"operation exited {process.returncode}: {_operation_tail(work / 'stderr.log')}"
        )
    result = _result_object(work / "result.json")
    deferred = _deferral(result)
    if deferred is not None:
        raise deferred
    return result


def _launch(
    module: str, work: Path, env: dict[str, str], secrets: Mapping[str, str] | None
) -> subprocess.Popen[bytes]:
    with (work / "stdout.log").open("xb") as stdout, (work / "stderr.log").open("xb") as stderr:
        os.fchmod(stdout.fileno(), 0o600)
        os.fchmod(stderr.fileno(), 0o600)
        process = subprocess.Popen(  # noqa: S603 -- fixed trusted worker in this checkout
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
            start_new_session=True,
            stdin=subprocess.DEVNULL if secrets is None else subprocess.PIPE,
            stdout=stdout,
            stderr=stderr,
            env=env,
            cwd=_CODE_ROOT,
            close_fds=True,
        )
    if secrets is not None and process.stdin is not None:
        with suppress(OSError):
            process.stdin.write(json.dumps(dict(secrets)).encode())
        with suppress(OSError):
            process.stdin.close()
    return process


async def _await_operation(
    process: subprocess.Popen[bytes], stop: StopSignal | None, deadline: float, tail: _Tail | None
) -> None:
    while process.poll() is None:
        if tail is not None:
            tail.pump()
        if stop is not None and stop.is_set():
            raise RuntimeError("operation was stopped")
        if time.monotonic() >= deadline:
            raise TimeoutError("operation exceeded its execution bound")
        await asyncio.sleep(0.05)


def _stop_worker(process: subprocess.Popen[bytes], grace_s: float) -> None:
    """Bound stop of the known live worker/group; no family-disappearance proof."""
    if process.poll() is not None:
        return
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=3)


@dataclass(frozen=True)
class CompletedOperation:
    """A zero-exit worker result awaiting artifact validation and publication."""

    kind: OperationKind
    work: Path
    result: dict[str, object]
    held: ExitStack = field(default_factory=ExitStack, compare=False, repr=False)

    async def commit[T](self, accept: Callable[[], T]) -> T:
        """Validate/publish off the event loop, then remove private staging."""
        with self.held:
            try:
                return _complete(*await _to_completion(accept))
            finally:
                _complete(*await _to_completion(lambda: cleanup(self.kind, self.work)))
