"""Owned lifetime of one disposable ``execute_code`` process tree.

Each run has exactly one direct-child reap task, one domain-close task, and one
reader-join task. POSIX owns a process group. Windows owns a Job Object whose
handle survives root exit and kills every non-breakaway member when closed.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Literal

import psutil

from shared.log import logger
from shared.platform import IS_WINDOWS
from shared.winjob import WindowsJob

_READER_JOIN_TIMEOUT_S = 5.0
_EMERGENCY_SETTLE_TIMEOUT_S = 5.0
_ROOT_EXIT_POLL_S = 0.05


def _process_group_has_live_member(pgid: int) -> bool:
    """Whether the pinned POSIX group still contains a signalable process.

    macOS returns EPERM for ``killpg`` when a group contains only zombies. The
    unreaped root still pins the numeric pgid during this scan, so an empty-live
    answer cannot race a newly reused unrelated group.
    """
    for process in psutil.process_iter(["pid", "status"]):
        try:
            if os.getpgid(process.info["pid"]) != pgid:
                continue
            if process.info["status"] in {psutil.STATUS_DEAD, psutil.STATUS_ZOMBIE}:
                continue
            if process.info["status"] is None:
                raise psutil.AccessDenied(process.info["pid"])
            return True
        except (ProcessLookupError, psutil.NoSuchProcess):
            continue
    return False


@dataclass
class ExecProcessDomain:
    """The root-independent ownership handle for one exec process tree."""

    proc: subprocess.Popen[bytes]
    windows_job: WindowsJob | None

    def close_confirmed(self, deadline: float) -> None:
        """Close and observe managed members before the unreaped root pin ends.

        Only the dedicated domain owner calls this, while it still directly
        parents the unreaped root. Never use a historical numeric PGID after
        root reap, and never treat escaped sessions as proven domain members.
        """
        if IS_WINDOWS:
            if self.windows_job is None:
                raise RuntimeError("Windows exec process has no Job Object")
            self.windows_job.terminate_and_confirm(deadline)
            return
        self.close()
        while _process_group_has_live_member(self.proc.pid):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("exec group still has live managed members")
            time.sleep(min(_ROOT_EXIT_POLL_S, remaining))

    def close(self) -> None:
        """Hard-stop all remaining domain members. Called by one owner task."""
        if IS_WINDOWS:
            if self.windows_job is None:
                raise RuntimeError("Windows exec process has no Job Object")
            try:
                self.windows_job.close()
            except BaseException:
                # A failed Job close must not strand the direct root and make
                # the sole reap task wait forever. Descendant ownership is no
                # longer provable, so the original close error still wins.
                with contextlib.suppress(OSError):
                    self.proc.kill()
                raise
            return
        try:
            if _process_group_has_live_member(self.proc.pid):
                os.killpg(self.proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except PermissionError:
            # Members can all exit after the pre-scan; macOS then reports
            # EPERM for the zombie-only group. The pinned zombie prevents pgid
            # reuse, so a second no-live result makes this a successful no-op.
            if not _process_group_has_live_member(self.proc.pid):
                return
            with contextlib.suppress(OSError):
                self.proc.kill()
            raise
        except BaseException:
            with contextlib.suppress(OSError):
                self.proc.kill()
            raise


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

    def __init__(self, domain: ExecProcessDomain, root_exit_task: asyncio.Task[None]) -> None:
        self._domain = domain
        self._close_lock = threading.Lock()
        self._closed = False
        self._requested = asyncio.Event()
        self.task = asyncio.create_task(
            self._close_after_exit_or_request(root_exit_task),
            name=f"exec-domain-close-{domain.proc.pid}",
        )

    def request(self) -> None:
        self._requested.set()

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
            self._domain.close()
            self._closed = True

    def reap_now(self, timeout: float) -> int:
        """Bounded direct-child reap for the Runner-cancellation barrier."""
        return self._domain.proc.wait(timeout=timeout)

    async def wait(self) -> None:
        await asyncio.shield(self.task)

    async def _close_after_exit_or_request(self, root_exit_task: asyncio.Task[None]) -> None:
        request_task = asyncio.create_task(
            self._requested.wait(), name=f"exec-domain-stop-request-{self._domain.proc.pid}"
        )
        try:
            await asyncio.wait({root_exit_task, request_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if not request_task.done():
                request_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await request_task
        # POSIX close includes a process-table scan before killpg; Windows
        # enters the kernel Job API. Neither belongs on the agent event loop.
        await asyncio.to_thread(self.close_now)


def signal_child(proc: subprocess.Popen[bytes], sig: int, domain_close: DomainCloseOwner) -> None:
    """Ask the owned tree to stop; Windows Job Objects only provide hard stop."""
    if IS_WINDOWS:
        domain_close.request()
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, sig)


def start_root_exit_observer(proc: subprocess.Popen[bytes]) -> asyncio.Task[None]:
    """Observe root exit without reaping/releasing its POSIX pid or pgid.

    A gone POSIX process (``NoSuchProcess``) counts as exited.
    """
    identity = None if IS_WINDOWS else psutil.Process(proc.pid)

    def _observe() -> None:
        if IS_WINDOWS:
            while proc.poll() is None:
                time.sleep(_ROOT_EXIT_POLL_S)
            return
        assert identity is not None  # noqa: S101 — established by platform branch
        while True:
            try:
                status = identity.status()
            except psutil.NoSuchProcess:
                return
            if status in {psutil.STATUS_DEAD, psutil.STATUS_ZOMBIE}:
                return
            time.sleep(_ROOT_EXIT_POLL_S)

    return asyncio.create_task(asyncio.to_thread(_observe), name=f"exec-root-exit-{proc.pid}")


def start_reap(proc: subprocess.Popen[bytes], domain_close: DomainCloseOwner) -> asyncio.Task[int]:
    """Run the sole ``Popen.wait`` only after the process domain was closed."""

    async def _reap_after_domain_close() -> int:
        # The barrier reports close failure separately; still reap root.
        with contextlib.suppress(Exception):
            await domain_close.wait()
        return await asyncio.to_thread(proc.wait)

    return asyncio.create_task(_reap_after_domain_close(), name=f"exec-reap-{proc.pid}")


def start_reader_join(
    reap_task: asyncio.Task[int], reader: threading.Thread, pid: int
) -> asyncio.Task[None]:
    """Make one bounded reader join after the root's sole reap attempt."""

    async def _join_after_reap() -> None:
        # The barrier reports the reap failure separately; still join once.
        with contextlib.suppress(Exception):
            await asyncio.shield(reap_task)
        # Put the bound inside Thread.join: wait_for(to_thread(join)) cancels
        # only the Future and leaves the executor worker blocked indefinitely.
        await asyncio.to_thread(reader.join, _READER_JOIN_TIMEOUT_S)
        if reader.is_alive():
            raise RuntimeError(
                f"exec reader for pid {pid} remained alive after its process "
                f"domain closed and {_READER_JOIN_TIMEOUT_S}s join elapsed"
            )

    return asyncio.create_task(_join_after_reap(), name=f"exec-reader-join-{pid}")


async def wait_with_grace(
    proc: subprocess.Popen[bytes],
    root_exit_task: asyncio.Task[None],
    grace_s: float,
    domain_close: DomainCloseOwner,
) -> bool:
    """Give a POSIX signal its grace window; request hard stop on expiry.

    Windows requests Job close when the signal decision is made, so this wait
    only bounds direct-root reaping there. Resource errors are observed later
    by ``settle_resources`` and never short-circuit another stage.
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

    Priority is domain ownership, direct-root reap, then reader join. No stage
    failure cancels or skips another stage.
    """
    if request_stop:
        domain_close.request()
    stages: list[tuple[TeardownStage, asyncio.Future[Any]]] = [
        ("domain_close", domain_close.task),
        ("root_exit", root_exit_task),
        ("reap", reap_task),
    ]
    if reader_join_task is not None:
        stages.append(("reader_join", reader_join_task))
    results = await asyncio.gather(
        *(asyncio.shield(task) for _stage, task in stages),
        return_exceptions=True,
    )
    return tuple(
        TeardownFailure(stage, result)
        for (stage, _task), result in zip(stages, results, strict=True)
        if isinstance(result, BaseException)
    )


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
    """Finish the close→reap→reader barrier despite repeated cancellation."""

    async def _cleanup() -> tuple[TeardownFailure, ...]:
        return await settle_resources(
            root_exit_task,
            reap_task,
            domain_close,
            reader_join_task,
            request_stop=True,
        )

    cleanup = asyncio.create_task(_cleanup(), name=f"exec-teardown-{domain_close.pid}")
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            continue
    return await cleanup


def settle_cancelled_owners(
    domain_close: DomainCloseOwner,
    reader: threading.Thread | None,
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

    try:
        domain_close.reap_now(max(0.0, deadline - time.monotonic()))
    except BaseException as exc:
        failures.append(TeardownFailure("reap", exc))

    if reader is not None:
        try:
            reader.join(max(0.0, deadline - time.monotonic()))
        except BaseException as exc:
            failures.append(TeardownFailure("reader_join", exc))
        else:
            if reader.is_alive():
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
    "settle_cancelled_owners",
    "settle_resources",
    "signal_child",
    "start_reader_join",
    "start_reap",
    "start_root_exit_observer",
    "wait_with_grace",
]
