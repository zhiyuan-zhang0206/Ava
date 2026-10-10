"""Owned lifetime of one disposable ``execute_code`` process tree.

Each run has exactly one direct-child reap task, one domain-close task, and one
bounded output EOF tail task. The direct child owns a POSIX process group.
"""

from __future__ import annotations

import asyncio
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Literal

import psutil

from base.log import logger
from base.native_process.exec_domain import ExecProcessDomain as ExecProcessDomain

from ._output_pipe import ExecOutputPipe

_READER_JOIN_TIMEOUT_S = 5.0
_EMERGENCY_SETTLE_TIMEOUT_S = 5.0
_ROOT_EXIT_POLL_S = 0.05
_RESOURCE_SETTLE_TIMEOUT_S = 3 * _EMERGENCY_SETTLE_TIMEOUT_S


TeardownStage = Literal["domain_close", "root_exit", "reap", "reader_join"]


@dataclass(frozen=True)
class TeardownFailure:
    stage: TeardownStage
    error: BaseException


class ExecTeardownError(RuntimeError):
    """Every resource stage ran, but one or more could not be settled."""

    def __init__(self, failures: tuple[TeardownFailure, ...]) -> None:
        self.failures = failures
        detail = "; ".join(
            f"{failure.stage}: {type(failure.error).__name__}: {failure.error}"
            for failure in failures
        )
        super().__init__(f"execute_code teardown failed ({detail})")


class DomainCloseOwner:
    """Sole closer: non-reaping root-exit observation or hard stop triggers it."""

    def __init__(
        self, domain: ExecProcessDomain, root_exit_task: asyncio.Task[None] | None = None
    ) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()
        self._errors: list[BaseException] = []
        self.reap_task: asyncio.Task[int] | None = None
        self.reader_join_task: asyncio.Task[None] | None = None
        self.teardown_task: asyncio.Task[tuple[TeardownFailure, ...]] | None = None
        self._reader: ExecOutputPipe | None = None
        self._domain = domain
        if root_exit_task is None:
            root_exit_task = asyncio.create_task(
                observe_root_exit(domain.proc), name=f"exec-root-exit-{domain.proc.pid}"
            )
            self._register(root_exit_task)
        else:
            self._register(root_exit_task)
        self.root_exit_task = root_exit_task
        self._close_lock = threading.Lock()
        self._closed = False
        # A future, not an Event: `asyncio.wait` races it against the root exit directly.
        self._requested: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.task = asyncio.create_task(
            self._close_after_exit_or_request(root_exit_task),
            name=f"exec-domain-close-{domain.proc.pid}",
        )
        self._register(self.task)

    def _register(self, task: asyncio.Task[Any]) -> None:
        if task not in self._tasks:
            self._tasks.add(task)
            task.add_done_callback(self._completed)

    def _completed(self, task: asyncio.Task[Any]) -> None:
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                self._errors.append(error)
                if task is self.task and not self.root_exit_task.done():
                    self.root_exit_task.cancel()
                logger.opt(exception=error).error(
                    "exec resource task failed: {name}", name=task.get_name()
                )

    def _raise_failure(self) -> None:
        if self._errors:
            raise self._errors[0]

    async def stop(
        self, timeout: float, *, request_stop: bool = True
    ) -> tuple[asyncio.Task[Any], ...]:
        """Request native closure; retain unfinished tasks without cancelling native work."""
        if request_stop:
            self.request()
        pending = {task for task in self._tasks if task is not asyncio.current_task()}
        await asyncio.wait(pending, timeout=timeout)
        self._raise_failure()
        return self.unfinished

    async def finish_later(self) -> None:
        """The actual service keeps this same owner until every stage finishes."""
        await asyncio.wait(self._tasks)
        self._raise_failure()
        if any(task.cancelled() for task in self._tasks):
            raise RuntimeError("exec resource task was cancelled before settlement")

    @property
    def unfinished(self) -> tuple[asyncio.Task[Any], ...]:
        return tuple(task for task in self._tasks if not task.done())

    def start_reap(self) -> asyncio.Task[int]:
        if self.reap_task is None:
            self.reap_task = asyncio.create_task(
                self._reap_after_close(), name=f"exec-reap-{self.pid}"
            )
            self._register(self.reap_task)
        return self.reap_task

    def _bind_reap(self, task: asyncio.Task[int]) -> None:
        if self.reap_task is None:
            self.reap_task = task
        elif self.reap_task is not task:
            raise RuntimeError("exec reap belongs to another resource owner")
        self._register(task)

    async def _reap_after_close(self) -> int:
        await self.wait()
        return await asyncio.to_thread(self.reap_now, _EMERGENCY_SETTLE_TIMEOUT_S)

    def start_reader_join(
        self, reap_task: asyncio.Task[int], reader: ExecOutputPipe
    ) -> asyncio.Task[None]:
        self._bind_reap(reap_task)
        if self.reader_join_task is not None:
            if reader is not self._reader:
                raise RuntimeError("exec reader join belongs to another output pipe")
            return self.reader_join_task
        self._reader = reader
        self.reader_join_task = asyncio.create_task(
            self._join_after_reap(reap_task, reader), name=f"exec-reader-join-{self.pid}"
        )
        self._register(self.reader_join_task)
        return self.reader_join_task

    async def _join_after_reap(self, reap_task: asyncio.Task[int], reader: ExecOutputPipe) -> None:
        await asyncio.wait({reap_task})
        await reader.finish(_READER_JOIN_TIMEOUT_S)
        if not reader.closed:
            raise RuntimeError(
                f"exec reader for pid {self.pid} remained alive after its process "
                f"domain closed and {_READER_JOIN_TIMEOUT_S}s join elapsed"
            )

    def start_teardown(
        self, reap_task: asyncio.Task[int], reader_join_task: asyncio.Task[None] | None
    ) -> asyncio.Task[tuple[TeardownFailure, ...]]:
        self._bind_reap(reap_task)
        if self.teardown_task is not None:
            return self.teardown_task
        self.teardown_task = asyncio.create_task(
            settle_resources(
                self.root_exit_task, reap_task, self, reader_join_task, request_stop=True
            ),
            name=f"exec-teardown-{self.pid}",
        )
        self._register(self.teardown_task)
        return self.teardown_task

    def request(self) -> None:
        if not self._requested.done():
            self._requested.set_result(None)

    @property
    def pid(self) -> int:
        return self._domain.proc.pid

    @property
    def interrupted(self) -> bool:
        """Whether Runner cancellation has reached this ownership task."""
        return self.task.cancelled() or self.task.cancelling() > 0

    def close_now(self) -> None:
        """Close the domain exactly once without requiring a live event loop."""
        with self._close_lock:
            if self._closed:
                return
            self._domain.close_confirmed(time.monotonic() + _EMERGENCY_SETTLE_TIMEOUT_S)
            self._closed = True

    def reap_now(self, timeout: float) -> int:
        """Bounded direct-child reap for the Runner-cancellation barrier."""
        with self._close_lock:
            if not self._closed:
                raise RuntimeError("exec leader remains pinned after unresolved closure")
            return self._domain.proc.wait(timeout=timeout)

    def signal_now(self, signum: int) -> None:
        with self._close_lock:
            if not self._closed:
                self._domain.signal(signum)

    async def wait(self) -> None:
        await asyncio.shield(self.task)

    async def _close_after_exit_or_request(self, root_exit_task: asyncio.Task[None]) -> None:
        await asyncio.wait({root_exit_task, self._requested}, return_when=asyncio.FIRST_COMPLETED)
        # Native close and bounded member observation run outside the agent loop.
        await asyncio.to_thread(self.close_now)


def signal_child(proc: subprocess.Popen[bytes], sig: int, domain_close: DomainCloseOwner) -> None:
    """Ask the owned tree to stop."""
    if proc.pid != domain_close.pid:
        raise RuntimeError("exec signal belongs to another direct owner")
    domain_close.signal_now(sig)


async def observe_root_exit(proc: subprocess.Popen[bytes]) -> None:
    """Observe root exit without reaping/releasing its POSIX pid or pgid.

    A gone POSIX process (``NoSuchProcess``) counts as exited.
    """
    identity = psutil.Process(proc.pid)

    while True:
        try:
            status = identity.status()
        except psutil.NoSuchProcess:
            return
        if status in {psutil.STATUS_DEAD, psutil.STATUS_ZOMBIE}:
            return
        await asyncio.sleep(_ROOT_EXIT_POLL_S)


def start_reap(proc: subprocess.Popen[bytes], domain_close: DomainCloseOwner) -> asyncio.Task[int]:
    """The domain owner retains the sole reap task."""
    if proc is not domain_close._domain.proc:
        raise RuntimeError("exec reap belongs to another direct owner")
    return domain_close.start_reap()


def start_reader_join(
    reap_task: asyncio.Task[int], reader: ExecOutputPipe, domain_close: DomainCloseOwner
) -> asyncio.Task[None]:
    """The domain owner retains the bounded output tail task."""
    return domain_close.start_reader_join(reap_task, reader)


async def wait_with_grace(
    proc: subprocess.Popen[bytes],
    root_exit_task: asyncio.Task[None],
    grace_s: float,
    domain_close: DomainCloseOwner,
) -> bool:
    """Give a POSIX signal its grace window; request hard stop on expiry.

    Resource errors are observed later by ``settle_resources`` and never
    short-circuit another stage.
    """
    done, _pending = await asyncio.wait({root_exit_task}, timeout=grace_s)
    if done:
        return True
    domain_close.request()
    logger.warning(
        "[{label}] exec child {pid} survived the {grace}s grace period — "
        "hard-stopped its process domain (native-stuck code or swallowed signal)",
        label="exec-subprocess-killed",
        pid=proc.pid,
        grace=grace_s,
        event="exec_subprocess_killed",
    )
    return False


async def settle_resources(
    root_exit_task: asyncio.Task[None],
    reap_task: asyncio.Task[int],
    domain_close: DomainCloseOwner,
    reader_join_task: asyncio.Task[None] | None,
    *,
    request_stop: bool,
) -> tuple[TeardownFailure, ...]:
    """Observe every cleanup owner, then return failures in stable priority.

    Failed closure stops exit observation and blocks reap; the bounded output
    tail still runs. The retained child is unresolved, never declared exited.
    """
    domain_close._bind_reap(reap_task)
    if reader_join_task is not None:
        domain_close._register(reader_join_task)
    stop_error: Exception | None = None
    try:
        await domain_close.stop(_RESOURCE_SETTLE_TIMEOUT_S, request_stop=request_stop)
    except Exception as error:
        stop_error = error
    stages: list[tuple[TeardownStage, asyncio.Future[Any]]] = [
        ("domain_close", domain_close.task),
        ("root_exit", root_exit_task),
        ("reap", reap_task),
    ]
    if reader_join_task is not None:
        stages.append(("reader_join", reader_join_task))
    failures: list[TeardownFailure] = []
    for stage, task in stages:
        if not task.done():
            failures.append(
                TeardownFailure(stage, TimeoutError(f"exec {stage} remains unfinished"))
            )
        else:
            try:
                task.result()
            except BaseException as error:
                failures.append(TeardownFailure(stage, error))
    if stop_error is not None and not any(item.error is stop_error for item in failures):
        failures.append(TeardownFailure("domain_close", stop_error))
    return tuple(failures)


def annotate_original_failure(
    original: BaseException, failures: tuple[TeardownFailure, ...]
) -> None:
    """Keep the work error primary while making cleanup failures visible."""
    if failures:
        original.add_note(str(ExecTeardownError(failures)))


async def finish_teardown_despite_cancellation(
    root_exit_task: asyncio.Task[None],
    reap_task: asyncio.Task[int],
    domain_close: DomainCloseOwner,
    reader_join_task: asyncio.Task[None] | None,
) -> tuple[TeardownFailure, ...]:
    """Finish the close→reap→output EOF barrier despite repeated cancellation."""

    if root_exit_task is not domain_close.root_exit_task:
        raise RuntimeError("exec teardown belongs to another root observer")
    cleanup = domain_close.start_teardown(reap_task, reader_join_task)
    deadline = asyncio.get_running_loop().time() + _RESOURCE_SETTLE_TIMEOUT_S
    while not cleanup.done() and asyncio.get_running_loop().time() < deadline:
        try:
            await asyncio.wait(
                {cleanup}, timeout=max(0, deadline - asyncio.get_running_loop().time())
            )
        except asyncio.CancelledError:
            continue
    if cleanup.done():
        return cleanup.result()
    return (TeardownFailure("domain_close", TimeoutError("exec teardown remains unfinished")),)


def settle_cancelled_owners(
    domain_close: DomainCloseOwner,
    reader: ExecOutputPipe | None,
) -> tuple[TeardownFailure, ...]:
    """Synchronously settle resources whose async owners Runner cancelled.

    ``asyncio.Runner`` cancels every task when a signal handler raises
    ``SystemExit``.  At that point another coroutine or ``to_thread`` call
    cannot own teardown: the new task is cancelled too, while its executor
    worker can keep Runner shutdown blocked.  This emergency barrier therefore
    uses only bounded synchronous operations.  It is valid only after the
    domain-close owner itself is already cancelled.
    """
    if not domain_close.interrupted:
        raise RuntimeError("cancelled-owner settlement requires a cancelled domain owner")

    failures: list[TeardownFailure] = []
    deadline = time.monotonic() + _EMERGENCY_SETTLE_TIMEOUT_S
    try:
        domain_close.close_now()
    except BaseException as exc:
        failures.append(TeardownFailure("domain_close", exc))

    if not failures:
        try:
            domain_close.reap_now(max(0.0, deadline - time.monotonic()))
        except BaseException as exc:
            failures.append(TeardownFailure("reap", exc))

    if reader is not None:
        try:
            reader.finish_now(max(0.0, deadline - time.monotonic()))
        except BaseException as exc:
            failures.append(TeardownFailure("reader_join", exc))
        else:
            if not reader.closed:
                failures.append(
                    TeardownFailure(
                        "reader_join",
                        RuntimeError(
                            f"exec reader for pid {domain_close.pid} remained alive after "
                            f"its process domain closed and the "
                            f"{_EMERGENCY_SETTLE_TIMEOUT_S}s emergency deadline elapsed"
                        ),
                    )
                )
    return tuple(failures)


__all__ = [
    "_READER_JOIN_TIMEOUT_S",
    "DomainCloseOwner",
    "ExecProcessDomain",
    "ExecTeardownError",
    "TeardownFailure",
    "annotate_original_failure",
    "finish_teardown_despite_cancellation",
    "observe_root_exit",
    "settle_cancelled_owners",
    "settle_resources",
    "signal_child",
    "start_reader_join",
    "start_reap",
    "wait_with_grace",
]
