"""Cluster identity helpers shared by observability producers and readers."""

import os
from pathlib import Path
from typing import Literal

ObservabilityEndpointVariable = Literal[
    "AVA_TELEMETRY_OTLP_ENDPOINT",
    "AVA_TELEMETRY_LOKI_URL",
]


def cluster_label(home: Path | None = None) -> str:
    """Return the process home's display label without touching the data plane.

    ``home_label`` is the canonical identity presented in telemetry. Startup
    diagnostics must not crash because identity formatting failed, so an
    unexpected resolver failure falls back to the stable home slug; if even
    resolving the home itself fails, the explicit ``.unknown`` label preserves
    the required dimension without inventing another source of identity.
    """
    from base import cluster, paths

    try:
        resolved_home = home if home is not None else paths.ava_home()
    except Exception as exc:
        _report_label_fallback("home resolution", exc)
        return ".unknown"
    try:
        return cluster.home_label(resolved_home)
    except Exception as exc:
        _report_label_fallback("home label", exc)
        try:
            return cluster.home_slug(resolved_home)
        except Exception as slug_exc:
            _report_label_fallback("home slug", slug_exc)
            return ".unknown"


def _report_label_fallback(stage: str, exc: BaseException) -> None:
    """A degraded cluster label is reported (first + every 50th: the label is
    resolved per event), never folded in silently."""
    from base.telemetry.emitter import report_sink_failure

    report_sink_failure(f"cluster label {stage}", exc)


def production_identity() -> bool:
    """Whether this process has the registered production identity.

    The default implicit collector belongs only to a configured machine running
    against the production ``~/.ava`` cluster. Tests, ad-hoc processes without a
    machine identity, and other homes must opt into their own explicit endpoint.
    """
    from base.cluster.machine import MachineNameMissing, machine_name

    try:
        machine_name()
    except MachineNameMissing:
        return False
    return cluster_label() == ".ava"


def endpoint_override_is_explicit(variable: ObservabilityEndpointVariable) -> bool:
    """Whether the process environment explicitly selected an endpoint.

    Settings always exposes a default loopback URL, so its resolved value cannot
    distinguish operator intent from the unsafe co-located-home default. This
    narrow presence check is therefore the authority for the escape hatch.
    """
    return variable in os.environ


def gateway_observability_home() -> Path | None:
    """Return this unit's home when it serves the gateway capability.

    Missing or invalid role identity is deliberately treated as non-gateway so
    bootstrap and maintenance processes retain their historical behavior.
    """
    from base.cluster.machine import MachineRoleInvalid, MachineRoleMissing, machine_role
    from base.paths import ava_home

    try:
        roles = machine_role()
    except (MachineRoleMissing, MachineRoleInvalid):
        return None
    return ava_home() if "gateway" in roles else None


def home_is_observability_station(home: Path) -> bool:
    """Whether this home is the observability station — the provider identity
    of the host's native LGTM backends.

    Two equivalent forms: the legacy ``$AVA_HOME/lgtm-host`` marker (operator
    designation via ``ava lgtm on``) and the declarative ``observability-station``
    unit capability. The capability form resolves through the process's own
    machine identity and only counts when the resolved home IS this ``home`` —
    a dev worktree home never inherits the prod station's capability. Role
    resolution failure (unconfigured unit, bootstrap process) falls back to
    marker-only so every pre-existing call site keeps historical behavior.

    The single decision behind the converge bring-up/rendering steps, the
    backend roster gate, the producer OTLP export gate, the
    collector-lifecycle gates, and the Loki read gate — they cannot drift apart
    again (issue #622).
    """
    if (home / "lgtm-host").exists():
        return True
    from base.cluster.machine import (
        MachineRoleInvalid,
        MachineRoleMissing,
        is_observability_station,
    )
    from base.paths import ava_home

    try:
        if not is_observability_station():
            return False
    except (MachineRoleMissing, MachineRoleInvalid):
        return False
    return home.resolve() == ava_home().resolve()


def collector_allowed_for_home(home: Path | None) -> bool:
    """Whether the gateway home at ``home`` may run the local otel-collector.

    The observability station (``lgtm-host`` marker or ``observability-station``
    capability) always runs the sidecar. A non-station gateway runs it only when
    the operator explicitly overrode ``AVA_TELEMETRY_OTLP_ENDPOINT`` — the same
    escape hatch the exporter side honors
    (``base.telemetry.otlp.telemetry_otlp.observability_export_allowed``) — so an explicit
    collector export is not silently starved of its local sidecar. ``None`` (no
    gateway home: pure runner, unconfigured unit, bootstrap) keeps historical
    behavior — relay collectors are not gated.

    One decision shared by the three collector-lifecycle paths — the roster
    gate (``ops.spec._otel_collector_gate_reason``), the converge step
    (``cli.commands.observability.otel_collector.ensure_otel_collector_step``), and the
    sidecar healthcheck (``services.supervision.healthchecks.otel_collector.
    _collector_serves_this_home``) — so they cannot drift apart again
    (issue #622).
    """
    if home is None:
        return True
    return home_is_observability_station(home) or endpoint_override_is_explicit(
        "AVA_TELEMETRY_OTLP_ENDPOINT"
    )
