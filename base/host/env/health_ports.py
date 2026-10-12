"""The health daemons' port table: where each one listens by default, and how a config reader overrides it.

Settings-free on purpose (it takes the caller's field reader), so the telemetry alerts, the
daemons' endpoint table (`base.daemon.endpoints`) and the roster all resolve a port the same way.
"""

from __future__ import annotations

from collections.abc import Callable

from base.host.env.port_table import FIXED_PORTS
from base.host.env.registry import health_port_env_aliases

# Three tables describe the same fact (daemon health ports) and are derived,
# never hand-maintained:
#
#   health_port_env_aliases() (host.env.registry) — service -> env var, the derive surface
#   DEFAULT_PORTS (below)                         — the fixed-table subset for health daemons
#   HEALTH_PORT_OVERRIDES (below)                — service -> settings field
#
# `base/cluster/tests/test_cluster_env.py` guards that they stay in sync.

# The health daemons' ports from the fixed table (base.host.env.port_table, 8103-8116).
# The curl_url of ServiceSpec in cli/commands.py references the same ports. A
# unit whose `.env` names no health port binds these.
DEFAULT_PORTS: dict[str, int] = {svc: FIXED_PORTS[svc] for svc in health_port_env_aliases()}

# Settings field mapping — name -> settings attribute. Every health daemon's
# settings field follows `f"{name}_health_port"`, so the mapping is derived
# from _HEALTH_PORT_ENV rather than maintained twice.
HEALTH_PORT_OVERRIDES: dict[str, str] = {
    svc: f"{svc}_health_port" for svc in health_port_env_aliases()
}


def health_port(name: str, *, read_field: Callable[[str], int | None]) -> int:
    """Resolve a daemon's registered health port through the caller's config reader.

    First reads ``settings.<name>_health_port`` (overridable by env
    ``AVA_<NAME>_HEALTH_PORT``); falls back to ``DEFAULT_PORTS``; if
    the name is not registered, raises ``KeyError`` (fail fast; new
    daemons must register a port first; silent fallback is not
    allowed).
    """
    override_attr = HEALTH_PORT_OVERRIDES.get(name)
    override = None if override_attr is None else read_field(override_attr)
    return DEFAULT_PORTS[name] if override is None else int(override)
