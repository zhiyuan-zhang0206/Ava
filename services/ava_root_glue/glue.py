"""Reference wiring: assemble the root's health + self-check from the roster.

This is the deployment-side assembly the daemon's `--wiring` hook drives
(`--wiring services.ava_root_glue.glue:build_wiring`). It is deliberately
thin: probes come from the roster specs — the old watchdog's
`healthcheck_module` gate, with the shared identity probe `register_specs`
requires on top (a spec missing that probe is skipped — the one deliberate
rule delta), the two monitors run at their 60s defaults, and their snapshots
attach to the supervisor's status surface through the W1.2b seams.

The metric slots stay empty here: attribution coverage and reseeding latency
are adapter/window inputs (W1.3 / G4), and an empty slot reads `unavailable`
— never a fake number (W1.2b decision 4).

Deployment freedom lives here, not in the root package: a spec-less unit is
one `STATIC_PROBES` entry (`"module:attribute"`, resolved lazily), and a
drill can assemble over its own probe source (see `drill.py`).
"""

from __future__ import annotations

from collections.abc import Mapping

from ops.roster import build_services
from services.ava_root.health import HealthConfig, HealthMonitor
from services.ava_root.probes import ProbeRegistry
from services.ava_root.selfcheck import SelfCheckConfig, TreeSelfCheck
from services.ava_root.wiring import WiringContext, WiringParticipant

STATIC_PROBES: Mapping[str, str] = {}
"""Spec-less unit id -> `"module:attribute"` probe reference.

v1 is empty on purpose: the host-policy / data-plane / native-stack class is
registered here when the wiring slice (W1.3) names its probe modules; the
reference is validated eagerly and imported lazily (the `register_ref`
contract).
"""


def build_wiring(context: WiringContext) -> list[WiringParticipant]:
    """The reference participant set: health monitor + tree self-check."""
    registry = ProbeRegistry()
    registry.register_specs(build_services())
    for unit_id, ref in STATIC_PROBES.items():
        registry.register_ref(unit_id, ref)
    return assemble(context, registry)


def assemble(
    context: WiringContext,
    registry: ProbeRegistry,
    *,
    health_config: HealthConfig | None = None,
    selfcheck_config: SelfCheckConfig | None = None,
) -> list[WiringParticipant]:
    """Build the two monitors over `registry` and attach their status surfaces."""
    monitor = HealthMonitor(context.supervisor, registry, config=health_config)
    check = TreeSelfCheck(context.supervisor, config=selfcheck_config)
    context.supervisor.attach_health(monitor)
    context.supervisor.attach_metrics(check)
    return [monitor, check]
