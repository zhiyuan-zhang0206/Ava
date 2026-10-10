"""The event stream's bounded queue, single writer and finite completion barriers.

Construction starts the owned drain worker. Roots retain a lazy constructor and
invoke it only when an actual producer enqueues, so unused owners stay cold.
"""

from __future__ import annotations

import contextlib
import math
import queue
import sys
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

from base.telemetry.delivery.receipts import (
    DrainPhase,
    DrainResult,
    DrainStatus,
    Event,
    SyncReceipt,
)

__all__ = ["EventPipeline"]


class EventPipeline:
    """Bounded queue + drain thread owning all event persistence for the process.

    Same shape as the former loguru Postgres sink (which this replaces): the
    queue bound is the backpressure, the drain thread batches, and shed records
    are counted and reported as one `event_log_drop` event per flush so the ops
    monitor panel keeps its backlog metric."""

    def __init__(
        self,
        *,
        writer: Callable[[list[Event]], None],
        batch_size: int = 100,
        flush_interval_s: float = 0.5,
        queue_maxsize: int = 10_000,
    ) -> None:
        self._writer = writer
        self._batch_size = batch_size
        self._flush_interval_s = flush_interval_s
        self._queue: queue.Queue[Event | SyncReceipt] = queue.Queue(maxsize=queue_maxsize)
        self._drop_reported_at = 0.0
        self._drop_example: Event | None = None
        self.dropped = 0  # records shed because the queue was full since the last flush
        # enqueue() runs on producer threads while _flush() (drain thread)
        # reads and zeroes the counter — `+=` is not atomic under the GIL, so
        # the read-modify-write pair is serialized.
        self._dropped_lock = threading.Lock()
        self._admission_lock = threading.Lock()
        self._stop_requested = threading.Event()
        self._finished = threading.Event()
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._drain, daemon=True, name="event-emitter")
        self._thread.start()

    @property
    def stopped(self) -> bool:
        """True only after requested shutdown has joined this owned worker.

        Completion does not erase a terminal error; repeated stop collects it.
        """
        return self._stop_requested.is_set() and not self._thread.is_alive()

    def enqueue(self, event: Event) -> None:
        """Producer path: shed full, closed or failed admission without waiting on writes."""
        if not self._admit(event):
            self._record_drop(event)

    def _admit(self, event: Event | SyncReceipt) -> bool:
        with self._admission_lock:
            if self._stop_requested.is_set() or self._finished.is_set():
                return False
            try:
                self._queue.put_nowait(event)
                return True
            except queue.Full:
                return False

    def _record_drop(self, event: Event) -> None:
        with self._dropped_lock:
            self.dropped += 1
            self._drop_example = replace(event, ts=datetime.now(UTC))
            now = time.monotonic()
            due = self.dropped == 1 or now - getattr(self, "_drop_reported_at", 0.0) >= 5
            if due:
                self._drop_reported_at = now
        if due:
            from base.telemetry.loss import report_loss

            report_loss(event, 1, "emitter")

    def flush(self) -> DrainResult:
        """Acknowledge queued and held records through the sole drain writer."""
        return self.sync()

    def _check_error(self) -> None:
        if self._error is not None:
            raise self._error

    def _result(self, *, completed: bool, phase: DrainPhase) -> DrainResult:
        self._check_error()
        result = DrainResult(DrainStatus.COMPLETED if completed else DrainStatus.UNFINISHED, phase)
        if not completed:
            from base.telemetry.emitter import report_no_pipeline

            report_no_pipeline(
                "[event-emitter] {operation} timed out; telemetry shutdown degraded: "
                "unfinished ordinary records may be lost or land later",
                operation="stop()" if phase is DrainPhase.STOP else "sync()",
            )
        return result

    def sync(self, timeout: float = 5.0, *, bounded: bool = False) -> DrainResult:
        """Wait on a distinct FIFO receipt within one end-to-end deadline.

        All ordinary telemetry barriers are finite. ``bounded`` remains accepted
        for existing close callers. A stuck writer is never rescued by another
        writer; unfinished delivery is reported and returned to the caller.
        A terminal worker failure is raised with its original exception.
        """
        del bounded  # retained call compatibility; every ordinary barrier is now finite
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("event drain timeout must be finite and non-negative")
        if threading.current_thread() is self._thread:
            raise RuntimeError("event drain cannot wait on its own barrier")
        deadline = time.monotonic() + timeout
        receipt = SyncReceipt()
        admitted = False
        while not admitted:
            self._check_error()
            if self._finished.is_set():
                return self._result(completed=True, phase=DrainPhase.DRAIN)
            admitted = self._admit(receipt)
            remaining = deadline - time.monotonic()
            if not admitted and remaining <= 0:
                return self._result(completed=False, phase=DrainPhase.MARKER)
            if not admitted:
                self._finished.wait(min(0.01, remaining))
        while not receipt.done.is_set():
            self._check_error()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._result(completed=False, phase=DrainPhase.DRAIN)
            receipt.done.wait(min(0.01, remaining))
        return self._result(completed=True, phase=DrainPhase.DRAIN)

    def stop(self, timeout: float = 5.0) -> DrainResult:
        """Close admission, request the owned worker's exit, and join finitely.

        The stop request never enters the bounded event queue. Even a full queue
        and blocked writer leave the caller with an explicit unfinished result.
        Repeated stop calls observe late completion or the original failure.
        """
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("event drain timeout must be finite and non-negative")
        deadline = time.monotonic() + timeout
        with self._admission_lock:
            self._stop_requested.set()
        if threading.current_thread() is self._thread:
            raise RuntimeError("event drain cannot join itself")
        self._thread.join(timeout=max(0.0, deadline - time.monotonic()))
        return self._result(completed=not self._thread.is_alive(), phase=DrainPhase.STOP)

    def _flush(self, batch: list[Event]) -> None:
        """Write loss summaries directly; a saturated queue cannot shed its own alarm."""
        with self._dropped_lock:
            n = self.dropped
            self.dropped = 0
            example = self._drop_example
            self._drop_example = None
        if n and example is not None:
            from base.telemetry.loss import loss_event

            batch = [*batch, loss_event(example, n, "emitter", dropped_at=example.ts)]
        if not batch:
            return
        self._writer(batch)

    def _drain(self) -> None:
        """Own all writes and retain terminal failures for barrier/stop callers."""
        try:
            self._run_drain()
        except BaseException as exc:
            from base.telemetry.emitter import report_no_pipeline

            self._error = exc
            report_no_pipeline("[event-emitter] drain failed: {err}", err=repr(exc), exc=exc)
            # Early emit-before-init callers may have no logging sink yet.
            with contextlib.suppress(OSError, ValueError):
                traceback.print_exception(exc, file=sys.stderr)
        finally:
            self._finished.set()

    def _run_drain(self) -> None:
        """Flush batches and receipts in FIFO order; stop drains closed admission."""
        batch: list[Event] = []
        deadline = time.monotonic() + self._flush_interval_s
        while True:
            stopping = self._stop_requested.is_set()
            timeout = 0.0 if stopping else min(0.05, max(0.0, deadline - time.monotonic()))
            try:
                event = self._queue.get(timeout=timeout)
            except queue.Empty:
                if stopping or time.monotonic() >= deadline:
                    self._flush(batch)
                    batch = []
                    deadline = time.monotonic() + self._flush_interval_s
                if stopping:
                    return
                continue
            if isinstance(event, SyncReceipt):
                self._flush(batch)
                batch = []
                event.done.set()
                deadline = time.monotonic() + self._flush_interval_s
                continue
            batch.append(event)
            if len(batch) >= self._batch_size:
                self._flush(batch)
                batch = []
                deadline = time.monotonic() + self._flush_interval_s
