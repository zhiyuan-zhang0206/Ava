"""Probes of what an exec child's exit and first record import; run by test_lazy_child_telemetry.

`python -I -B -X utf8 child_lazy_telemetry.py <probe>` runs one scenario in a fresh interpreter and
prints one JSON report on its last line. Every probe imports inside its own function: a
module-level import would change what the probe measures.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import Any


def zero_record_exit() -> list[str]:
    import agent.execution.child as exec_child  # the module under test

    exec_child.finalize_telemetry()  # the zero-record exit path

    watched = ("base.telemetry", "base.telemetry.otlp.telemetry_otlp")
    return sorted(
        name for name in sys.modules if name.startswith("opentelemetry") or name in watched
    )


def identity_child_record() -> dict[str, Any]:
    """The real identity-child arm path, then the exporter check.

    `init_logger` is the exact arm seam (file sink + event-pipeline sink + deferred OTLP arm);
    `emit_child_boot_timing` is the record that used to bring the exporter stack up within the
    first drain tick. A writer receipt observes that tick; the exporter's public flush reports the
    retained tail.
    """
    import time

    import agent.execution.child as exec_child
    from base.agents.context.clients import ClientSet
    from base.telemetry.delivery.receipts import DrainStatus
    from base.telemetry.otlp import telemetry_otlp_exporting

    backends: list[Any] = []

    def writer(events: list[Any], backend: Any) -> None:
        backends.append(backend)
        backend.export_batch(events)

    pipeline = telemetry_otlp_exporting.ExportingEventPipeline(
        writer=writer, export_allowed=lambda: True
    )
    clients = ClientSet(pipeline_factory=lambda: pipeline)
    exec_child.init_logger(
        999999,
        producer=clients.event_pipeline,
        machine_reader=lambda: "lazy-child-probe",
    )  # the real arm path (task #3816 M4b)
    # The boot clock is all the record reads; the SDK modules stay unimported.
    child = exec_child.ChildContext(
        meiva=None,  # pyright: ignore[reportArgumentType]
        boot_started_at=time.perf_counter(),
    )
    exec_child.emit_child_boot_timing(child, clients=clients)
    assert pipeline.sync(timeout=5.0).status is DrainStatus.COMPLETED
    assert backends
    unfinished = backends[0].flush(timeout=0).status is DrainStatus.UNFINISHED
    deferred = clients.exports_deferred()
    pipeline.stop(timeout=5.0)

    forbidden = (
        "opentelemetry.sdk._logs",
        "opentelemetry.exporter",
        "opentelemetry.proto",
        "requests",
        "urllib3",
        "google.protobuf",
    )
    loaded = sorted(name for name in sys.modules if name.startswith(forbidden))
    return {"deferred": deferred, "unfinished": unfinished, "forbidden": loaded}


PROBES: dict[str, Callable[[], object]] = {
    "zero_record_exit": zero_record_exit,
    "identity_child_record": identity_child_record,
}

if __name__ == "__main__":
    sys.stdout.write(json.dumps(PROBES[sys.argv[1]]()) + "\n")
