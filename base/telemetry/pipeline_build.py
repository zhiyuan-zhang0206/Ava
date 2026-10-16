"""Compose the event pipeline's writer from its sinks, and close them at process exit.

The emitter (`base.telemetry.emitter`) only enqueues into whatever pipeline a root binds; this
module is where the sinks meet: the JSONL mirror, the `telemetry_events` store, the
observed-metrics projection and the OTLP exporter. It sits above all of them, so none of them
needs to import the emitter's composition.
"""

from __future__ import annotations

import atexit
from collections.abc import Callable
from pathlib import Path
from typing import Any

from base.cluster.machine import MachineIdentity
from base.telemetry.delivery.mirror import append_jsonl
from base.telemetry.delivery.pipeline import EventPipeline
from base.telemetry.delivery.receipts import DrainStatus, Event
from base.telemetry.delivery.reporting import failure_isolated, report_no_pipeline
from base.telemetry.delivery.state import process_state


def write_batch(
    events: list[Event], *, database: Callable[[], Any], export: Callable[[list[Event]], None]
) -> None:
    """Mirror first, then the telemetry_events record, the compact metric projection
    and OTLP export.

    Persistence and exporter transport failures retain their sink isolation.
    Required identity defects propagate to the writer's failure receipt. The
    projection cannot turn an observation into a billing proof.
    """
    if not events:
        return
    append_jsonl(events)
    with failure_isolated("telemetry_events store"):
        from base.telemetry.event_store import store_events

        store_events(database(), events)
    try:
        from base.telemetry.metrics.observed_metrics import project_events

        project_events(database(), events)
    except Exception as exc:
        report_no_pipeline("[observed-metrics] sink unavailable: {err}", err=repr(exc))
    export(events)


def build_pipeline(
    *,
    database: Callable[[], Any],
    machine: MachineIdentity,
    home: Path,
    flush_interval_s: float = 0.5,
) -> EventPipeline:
    """Own the writer and lazy exporter with the root's database and machine.

    `flush_interval_s` is how long the drain thread holds a partial batch before it writes it.
    """
    from base.telemetry.otlp.telemetry_otlp import observability_export_allowed
    from base.telemetry.otlp.telemetry_otlp_exporting import ExportingEventPipeline

    return ExportingEventPipeline(
        writer=lambda events, exporter: write_batch(
            events, database=database, export=exporter.export_batch
        ),
        export_allowed=lambda: observability_export_allowed(machine, home=home),
        flush_interval_s=flush_interval_s,
    )


def drain_on_exit() -> None:
    """Close ordinary admission and finitely join its single writer at normal exit.

    The ordered exit seam closes downstream sinks only after the writer finishes.
    Providers leave shutdown to their worker; an unfinished writer is reported;
    process exit may shed its ordinary tail. ``hard_exit`` bypasses this hook.
    """
    pipeline = process_state["pipeline"]
    if pipeline is None:
        return
    result = pipeline.stop()
    if result.status is DrainStatus.UNFINISHED:
        return  # the owned writer still uses these sinks; process exit sheds its tail
    with failure_isolated("close observed-metrics projection"):
        from base.telemetry.metrics.observed_metrics import close_projection

        close_projection()
    with failure_isolated("close telemetry_events store"):
        from base.telemetry.event_store import close_store

        close_store()


atexit.register(drain_on_exit)
