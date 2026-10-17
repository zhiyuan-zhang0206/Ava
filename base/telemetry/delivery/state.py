"""The per-process emitter binding: pipeline, process name, agent, machine and cluster."""

from __future__ import annotations

import socket
from collections.abc import Callable
from typing import Any


def resolve_machine(name_reader: Callable[[], str] | None = None) -> str:
    """Resolve this event's machine dimension from its explicit owner reader.
    Ordinary diagnostics without a reader use the hostname so a row always
    carries the dimension.

    Only the documented absence (`MachineNameMissing`) falls back silently.
    Any OTHER failure propagates rather than pinning the owner to a wrong
    dimension. An unbound ordinary diagnostic uses the hostname; it never
    discovers a configured machine through a process-global identity reader."""
    from base.cluster.machine import MachineNameMissing

    try:
        return name_reader() if name_reader is not None else socket.gethostname()
    except MachineNameMissing:
        return socket.gethostname()


# ── pipeline state (per-process singleton) ────────────────────────────────────

# The emitter pipeline; None until explicit process initialization. Dict mutation avoids ruff
# PLW0603 the same way base/telemetry/tracing.py does.
process_state: dict[str, Any] = {
    "pipeline": None,
    "process": "unknown",
    "agent_id": None,
    "machine": None,
    "cluster": None,
    "jsonl_day": None,
}


def process_name() -> str:
    """This process's bound identity (`init_telemetry(process=...)`), the
    bounded dimension every telemetry record carries as `process`. The OTLP
    backend stamps it into the metrics Resource (`service.name=meiva-<process>`)
    so same-named counters from different process kinds cannot collide into
    one Prometheus series."""
    return str(process_state["process"])


def ambient_agent_id() -> int | None:
    """Default identity established at process startup, if this process owns an agent."""
    return process_state["agent_id"]
