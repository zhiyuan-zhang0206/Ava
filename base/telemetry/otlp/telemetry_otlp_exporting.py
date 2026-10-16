from __future__ import annotations

import time
from collections.abc import Callable

from base.telemetry.delivery.pipeline import EventPipeline
from base.telemetry.delivery.receipts import DrainResult, DrainStatus, Event
from base.telemetry.otlp.telemetry_otlp import OtlpBackend

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
        # ava:ref base/telemetry/otlp/docs/telemetry-otlp/exit-flush.ava.okf.md#two-wait-classes-one-deadline
        deadline = time.monotonic() + timeout
        result = super().stop(max(0.0, deadline - time.monotonic()))
        if result.status is DrainStatus.UNFINISHED:
            return result
        result = self._exporter.shutdown(timeout=max(0.0, deadline - time.monotonic()))
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
