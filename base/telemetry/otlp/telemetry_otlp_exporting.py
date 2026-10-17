from __future__ import annotations

import math
import time
from collections.abc import Callable

from base.telemetry.delivery.pipeline import EventPipeline
from base.telemetry.delivery.receipts import DrainResult, DrainStatus, Event
from base.telemetry.otlp.telemetry_otlp import OtlpBackend, remaining_time

__all__ = ["ExportingEventPipeline"]


class ExportingEventPipeline(EventPipeline):
    def __init__(
        self,
        *,
        writer: Callable[[list[Event], OtlpBackend], None],
        export_allowed: Callable[[], bool],
        flush_interval_s: float = 0.5,
    ) -> None:
        self._exporter = OtlpBackend(export_allowed=export_allowed)
        self._exports_finished = False
        super().__init__(
            writer=lambda events: writer(events, self._exporter), flush_interval_s=flush_interval_s
        )

    @property
    def stopped(self) -> bool:
        return super().stopped and self._exports_finished

    def stop(self, timeout: float = 5.0) -> DrainResult:
        # meiva:ref base/telemetry/otlp/docs/telemetry-otlp/exit-flush.meiva.okf.md#two-wait-classes-one-deadline
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("event drain timeout must be finite and non-negative")
        deadline = time.monotonic() + timeout
        result = super().stop(remaining_time(deadline, time.monotonic()))
        if result.status is DrainStatus.UNFINISHED:
            return result
        result = self._exporter.shutdown(timeout=remaining_time(deadline, time.monotonic()))
        self._exports_finished = result.status is DrainStatus.COMPLETED
        return result

    def defer_exports(self) -> None:
        self._exporter.defer_until_exit()

    def finalize_exports(self) -> None:
        self._exporter.finalize()

    def warmup_exports(self) -> None:
        self._exporter.warmup()

    def exports_deferred(self) -> bool:
        return self._exporter.deferred_state()

    def record_export_loss(self, report: Event) -> None:
        self._exporter.record_loss(report)
