"""Queue-loss diagnostics that never re-enter a saturated event queue."""

from __future__ import annotations

import logging
import sys
from dataclasses import replace
from datetime import UTC, datetime

from base.log import logger
from base.telemetry import Event


def _stderr(message: str) -> None:
    """Write one diagnostic line to stderr, the channel that needs no configured sink.

    A closed or detached stderr (`OSError` / `ValueError`) is the one expected
    failure: a daemon whose stderr is gone has no further channel to report on.
    """
    try:
        sys.stderr.write(message + "\n")
    except (OSError, ValueError):
        return


def loss_event(
    event: Event, count: int, queue_name: str, *, dropped_at: datetime | None = None
) -> Event:
    """Preserve the affected process identity and mark actual data loss as error."""
    now = datetime.now(UTC)
    return replace(
        event,
        ts=now,
        agent_id=None,
        trace_id=None,
        span_id=None,
        category="telemetry",
        event_name="event_log_drop",
        level="error",
        attributes={
            "n": count,
            "queue": queue_name,
            "last_dropped_at": (dropped_at or now).timestamp(),
        },
    )


def report_loss(event: Event, count: int, queue_name: str) -> Event:
    """File/stderr diagnostic plus an independent metric; neither uses a log queue."""
    report = loss_event(event, count, queue_name)
    # Always available, even before logging/provider initialization in bare scripts.
    # This exceptional data-loss diagnostic must not depend on any configured sink.
    _stderr(f"ERROR: Telemetry queue {queue_name} full: lost {count} event(s)")
    try:
        logger.bind(_no_emitter=True).error(
            "Telemetry queue {queue} full: lost {n} event(s); event history is incomplete",
            queue=queue_name,
            n=count,
        )
    except Exception as exc:
        _stderr(f"ERROR: Telemetry loss log for queue {queue_name} failed: {exc!r}")
    try:
        from base.telemetry.otlp import telemetry_otlp

        backend = telemetry_otlp.backend
        if backend._meter is not None:
            metric_report = report
            if queue_name == "emitter":
                metric_report = replace(
                    report, attributes={k: v for k, v in report.attributes.items() if k != "n"}
                )
            backend._record_metrics(metric_report)
    except Exception as exc:
        _stderr(f"ERROR: Telemetry loss metric for queue {queue_name} failed: {exc!r}")
    return report


class _ExporterDropFilter(logging.Filter):
    """Observe the pinned OTel SDK's queue-overflow warning at the source."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.msg != "Queue full, dropping %s.":
            return True
        try:
            from base import telemetry

            now = datetime.now(UTC)
            state = telemetry._state
            event = Event(
                ts=now,
                trace_id=None,
                span_id=None,
                agent_id=None,
                machine=state["machine"] or telemetry._resolve_machine(),
                cluster=state["cluster"] or "",
                process=telemetry.process_name(),
                category="telemetry",
                event_name="event_log_drop",
                level="error",
                source="system",
                target_agent_id=None,
            )
            report = report_loss(event, 1, "otel-sdk")
            telemetry._append_jsonl([report])
        except Exception as exc:
            _stderr(f"ERROR: OTel SDK queue-overflow loss could not be recorded: {exc!r}")
        # The replacement error already reached local sinks. Re-exporting the
        # original warning through the log queue would amplify the overflow.
        return False


def install_exporter_drop_observer() -> None:
    """Attach once before constructing providers, including standalone SDK processes."""
    sdk_logger = logging.getLogger("opentelemetry.sdk._shared_internal")
    if not any(isinstance(item, _ExporterDropFilter) for item in sdk_logger.filters):
        # Run before OTel's DuplicateFilter: every lost record must count.
        sdk_logger.filters.insert(0, _ExporterDropFilter())
