"""The OTLP backend's single log writer and SDK resource lifecycle owner."""

from __future__ import annotations

import math
import queue
import threading
import time
from collections.abc import Callable
from typing import Any

from base.telemetry import (
    DrainPhase,
    DrainResult,
    DrainStatus,
    failure_isolated,
    report_no_pipeline,
)
from base.telemetry.otlp.telemetry_otlp_barrier import WorkerFlushMarker


class OtlpWorker:
    """Own one exporter attempt, including half-built providers and late cleanup."""

    def __init__(
        self,
        *,
        event_queue: queue.Queue[Any],
        initialize: Callable[[OtlpWorker], bool],
        emit_log: Callable[[Any], None],
        record_metrics: Callable[[Any], None],
        producers_idle: threading.Event,
        deferred_idle: threading.Event,
        deferred_active: Callable[[], bool],
    ) -> None:
        self._queue = event_queue
        self._initialize = initialize
        self._emit_log = emit_log
        self._record_metrics = record_metrics
        self._producers_idle = producers_idle
        self._deferred_idle = deferred_idle
        self._deferred_active = deferred_active
        self._deferred_replayed = False
        self.logs: Any = None
        self.metrics: Any = None
        self.metric_reader: Any = None
        self.initialized = False
        self._stop_requested = threading.Event()
        self.ready = threading.Event()
        self.finished = threading.Event()
        self._error: BaseException | None = None
        self._cleanup_error: BaseException | None = None
        self._reported = False
        self._cleanup_complete = False
        self._thread = threading.Thread(target=self._run, daemon=True, name="otlp-exporter")
        self._thread.start()

    def _report_error(self, error: BaseException) -> None:
        if not self._reported:
            self._reported = True
            report_no_pipeline("[otlp-exporter] worker failed: {err}", err=repr(error), exc=error)

    def _run(self) -> None:
        try:
            self._work()
        except BaseException as exc:
            self._error = exc
            self._report_error(exc)
        finally:
            self.ready.set()
            self.finished.set()

    def _work(self) -> None:
        try:
            self.initialized = self._initialize(self)
            self.ready.set()
            if self.initialized:
                self._drain()
        except BaseException as exc:
            self._error = exc
            self._report_error(exc)
        try:
            self._close_providers()
        except BaseException as exc:
            self._cleanup_error = exc
            report_no_pipeline("[otlp-exporter] cleanup failed: {err}", err=repr(exc), exc=exc)
        if self._error is not None:
            raise self._error
        if self._cleanup_error is not None:
            raise self._cleanup_error

    def _drain(self) -> None:
        while True:
            if self._deferred_active() and not self._stop_requested.is_set():
                self._deferred_idle.wait()
                if self._deferred_active():
                    self._stop_requested.wait(0.05)
                    continue
            if self._stop_requested.is_set():
                self._producers_idle.wait()
                self._deferred_idle.wait()
                self._replay_deferred_metrics()
            try:
                event = self._queue.get(timeout=0.05)
            except queue.Empty:
                if self._stop_requested.is_set():
                    return
                continue
            if event is None:
                return
            if isinstance(event, WorkerFlushMarker):
                self._flush_providers(timeout_millis=500)
                event.done.set()
                continue
            with failure_isolated("otlp log emit"):
                self._emit_log(event)

    def _replay_deferred_metrics(self) -> None:
        if self._deferred_replayed or not self._deferred_active():
            return
        self._deferred_replayed = True
        held: list[Any] = []
        while True:
            try:
                held.append(self._queue.get_nowait())
            except queue.Empty:
                break
        for event in held:
            if event is not None and not isinstance(event, WorkerFlushMarker):
                with failure_isolated("otlp deferred metric mapping"):
                    self._record_metrics(event)
        for event in held:
            self._queue.put_nowait(event)

    def _flush_providers(self, *, timeout_millis: int) -> None:
        for name, provider in (("logs", self.logs), ("metrics", self.metrics)):
            if provider is not None:
                with failure_isolated(f"otlp {name} flush"):
                    provider.force_flush(timeout_millis=timeout_millis)

    def _close_providers(self) -> None:
        # Direct metric producers can still be in an SDK call. Retaining their
        # providers until they finish avoids closing resources beneath them.
        self._producers_idle.wait()
        self._deferred_idle.wait()
        complete = True
        primary: BaseException | None = None
        providers = (("logs", self.logs), ("metrics", self.metrics))
        if self.metrics is None:
            providers += (("half-built metric reader", self.metric_reader),)
        for name, provider in providers:
            if provider is None:
                continue
            closed = False
            if name != "half-built metric reader":
                try:
                    with failure_isolated(f"otlp {name} flush"):
                        provider.force_flush(timeout_millis=2000)
                except BaseException as exc:
                    self._remember_cleanup_error(exc)
                    if primary is None:
                        primary = exc
            try:
                with failure_isolated(f"otlp {name} shutdown"):
                    provider.shutdown()
                    closed = True
            except BaseException as exc:
                self._remember_cleanup_error(exc)
                if primary is None:
                    primary = exc
            complete = complete and closed
        self._cleanup_complete = complete
        if primary is not None:
            raise primary

    def _remember_cleanup_error(self, error: BaseException) -> None:
        if self._cleanup_error is None:
            self._cleanup_error = error
        report_no_pipeline("[otlp-exporter] SDK teardown failed: {err}", err=repr(error), exc=error)

    def wait_ready(self) -> bool:
        self.ready.wait()
        self._check_error()
        return self.initialized and not self._stop_requested.is_set()

    def _check_error(self) -> None:
        if self._error is not None:
            raise self._error
        if self._cleanup_error is not None:
            raise self._cleanup_error

    def sync(self, timeout: float = 2.0) -> DrainResult:
        """Use the same writer's FIFO receipt; never flush SDK resources on a caller."""
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("OTLP timeout must be finite and non-negative")
        deadline = time.monotonic() + timeout
        self._check_error()
        marker = WorkerFlushMarker()
        if self._stop_requested.is_set() or self.finished.is_set():
            return self._result(
                phase=DrainPhase.DRAIN,
                completed=not self._thread.is_alive() and self._queue.empty(),
            )
        try:
            self._queue.put(marker, timeout=max(0.0, deadline - time.monotonic()))
        except queue.Full:
            return self._result(phase=DrainPhase.DRAIN, completed=False)
        while not marker.done.is_set():
            self._check_error()
            remaining = deadline - time.monotonic()
            if remaining <= 0 or self.finished.is_set():
                return self._result(phase=DrainPhase.DRAIN, completed=False)
            marker.done.wait(min(0.01, remaining))
        return self._result(phase=DrainPhase.DRAIN, completed=True)

    def request_stop(self) -> None:
        self._stop_requested.set()
        self.ready.set()  # wake an ensure caller even when SDK construction is stuck

    def stop(self, timeout: float = 2.0) -> DrainResult:
        """Close admission and join finitely; SDK/native cleanup may remain unfinished."""
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("OTLP timeout must be finite and non-negative")
        self._stop_requested.set()
        self.ready.set()
        self._thread.join(timeout=timeout)
        alive = self._thread.is_alive()
        return self._result(phase=DrainPhase.STOP, completed=not alive)

    def _result(self, *, phase: DrainPhase, completed: bool) -> DrainResult:
        self._check_error()
        if phase is DrainPhase.STOP:
            completed = completed and self._queue.empty() and self._cleanup_complete
        if not completed:
            report_no_pipeline("OTLP observation degraded; exporter work remains unfinished")
        return DrainResult(DrainStatus.COMPLETED if completed else DrainStatus.UNFINISHED, phase)
