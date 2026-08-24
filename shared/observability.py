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
    from shared import cluster, paths

    try:
        resolved_home = home if home is not None else paths.ava_home()
    except Exception:
        return ".unknown"
    try:
        return cluster.home_label(resolved_home)
    except Exception:
        try:
            return cluster.home_slug(resolved_home)
        except Exception:
            return ".unknown"


def production_identity() -> bool:
    """Whether this process has the registered production identity.

    The default implicit collector belongs only to a configured machine running
    against the production ``~/.ava`` cluster. Tests, ad-hoc processes without a
    machine identity, and other homes must opt into their own explicit endpoint.
    """
    from shared.machine import MachineNameMissing, machine_name

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
    from shared.machine import MachineRoleInvalid, MachineRoleMissing, machine_role
    from shared.paths import ava_home

    try:
        roles = machine_role()
    except (MachineRoleMissing, MachineRoleInvalid):
        return None
    return ava_home() if "gateway" in roles else None
