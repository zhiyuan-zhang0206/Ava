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
_ROOT_EXIT_POLL_S = 0.05


def _process_group_has_live_member(pgid: int) -> bool:
    """Whether the pinned POSIX group still contains a signalable process.

    macOS returns EPERM for ``killpg`` when a group contains only zombies. The
    unreaped root still pins the numeric pgid during this scan, so an empty-live
    answer cannot race a newly reused unrelated group.
    """
    for process in psutil.process_iter(["pid", "status"]):
        try:
            if process.info["status"] in {psutil.STATUS_DEAD, psutil.STATUS_ZOMBIE}:
                continue
            if os.getpgid(process.info["pid"]) == pgid:
                return True
        except (PermissionError, ProcessLookupError, psutil.Error):
            continue
    return False


@dataclass
class ExecProcessDomain:
    """The root-independent ownership handle for one exec process tree."""

    proc: subprocess.Popen[bytes]
    windows_job: WindowsJob | None

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
        await asyncio.to_thread(self._domain.close)


def signal_child(proc: subprocess.Popen[bytes], sig: int, domain_close: DomainCloseOwner) -> None:
    """Ask the owned tree to stop; Windows Job Objects only provide hard stop."""
    if IS_WINDOWS:
        domain_close.request()
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, sig)


def start_root_exit_observer(proc: subprocess.Popen[bytes]) -> asyncio.Task[None]:
    """Observe root exit without reaping/releasing its POSIX pid or pgid."""
    identity = None if IS_WINDOWS else psutil.Process(proc.pid)

    def _observe() -> None:
        if IS_WINDOWS:
            while proc.poll() is None:
                time.sleep(_ROOT_EXIT_POLL_S)
            return
        assert identity is not None  # noqa: S101 — established by platform branch
        while identity.status() not in {psutil.STATUS_DEAD, psutil.STATUS_ZOMBIE}:
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


__all__ = [
    "_READER_JOIN_TIMEOUT_S",
    "DomainCloseOwner",
    "ExecProcessDomain",
    "ExecTeardownError",
    "TeardownFailure",
    "annotate_original_failure",
    "finish_teardown_despite_cancellation",
    "settle_resources",
    "signal_child",
    "start_reader_join",
    "start_reap",
    "start_root_exit_observer",
    "wait_with_grace",
]
