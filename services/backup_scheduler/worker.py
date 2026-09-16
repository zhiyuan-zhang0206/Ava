"""Own interruptible backup jobs outside asyncio's non-cancellable executor.

The adoption gate prevents a worker from creating children before its controller
owns the process group. Cancellation unwinds the worker's synchronous finally
blocks, then bounds cleanup of that exact group before the daemon drops health
and its pidfile. No executor thread can keep interpreter shutdown waiting.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import queue
import signal
import time
import types
from collections.abc import Callable
from functools import partial
from typing import cast

from services.pitr.base_candidate import StopSignal
from services.pitr.worker_process import (
    WorkerQueue,
    enable_child_subreaper,
    group_members,
    reap_exited_group_children,
    reap_job_group,
    validate_ready_message,
    worker_bootstrap,
)
from shared.log import init_gateway_process


def _interrupt(signum: int, _frame: types.FrameType | None) -> None:
    # A repeated stop must not interrupt the job's finally blocks halfway through.
    signal.signal(signum, signal.SIG_IGN)
    raise KeyboardInterrupt


def _execute(job: Callable[[], object], _stop: StopSignal, output: WorkerQueue) -> None:
    signal.signal(signal.SIGTERM, _interrupt)
    try:
        init_gateway_process(name="pg-backup-worker")
        job()
    except BaseException as exc:
        output.put((False, f"{type(exc).__name__}: {exc}"[:4000]))
        raise
    else:
        output.put((True, ""))


async def run_job(job: Callable[[], object]) -> None:
    """Run one spawn-picklable job; cancellation reaps its owned process group.

    Scheduled restore jobs must use foreground Postgres: a pg_ctl-detached
    server would escape this ownership boundary. The spawn context also avoids
    inheriting the daemon's telemetry threads and database connections.
    """
    enable_child_subreaper()
    context = multiprocessing.get_context("spawn")
    stop = context.Event()
    adopted = context.Event()
    output = cast(WorkerQueue, context.Queue(maxsize=2))
    process = context.Process(
        target=worker_bootstrap, args=(partial(_execute, job), stop, output, adopted)
    )
    process.start()
    worker_pid = process.pid
    assert worker_pid is not None  # noqa: S101 -- a successfully started Process owns a PID
    pgid: int | None = None
    created_at: float | None = None
    try:
        deadline = time.monotonic() + 30
        while pgid is None:
            try:
                message = cast(tuple[str, str, str, str], output.get_nowait())
            except queue.Empty:
                if not process.is_alive() or time.monotonic() >= deadline:
                    raise RuntimeError("backup worker failed before ownership handshake") from None
                await asyncio.sleep(0.05)
                continue
            pgid, created_at = validate_ready_message(message, expected_pid=worker_pid)
            adopted.set()
        while process.is_alive():
            await asyncio.sleep(0.1)
        process.join()
        reap_exited_group_children(process, pgid)
        if group_members(pgid):
            raise RuntimeError("backup worker left live descendants")
        try:
            succeeded, detail = cast(tuple[bool, str], output.get(timeout=1))
        except queue.Empty as exc:
            raise RuntimeError("backup worker exited without a result") from exc
        if process.exitcode != 0 or not succeeded:
            raise RuntimeError(f"backup worker failed (exit={process.exitcode}): {detail}")
    finally:
        if pgid is not None and created_at is not None:
            reap_job_group(
                process,
                worker_pid=worker_pid,
                pgid=pgid,
                leader_created_at=created_at,
                grace_s=3,
                deadline_s=7,
            )
        elif process.is_alive():
            # The adoption gate guarantees this worker has created no descendants.
            process.kill()
            process.join(timeout=2)
        if process.is_alive():
            raise RuntimeError("backup worker could not be reaped")
        process.close()
        output.close()
        output.join_thread()
