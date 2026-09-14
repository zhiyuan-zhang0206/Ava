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

The macOS permissions helper rides the same static path but is registered
conditionally (`_helper_probe_enabled`): it exists only where the helper does.
Its probe detects and classifies — repair stays with the helper's own
lifecycle (task #3393).
"""

from __future__ import annotations

from collections.abc import Mapping

from ops.roster import build_services
from services.ava_root.health import HealthConfig, HealthMonitor
from services.ava_root.probes import ProbeRegistry
from services.ava_root.selfcheck import SelfCheckConfig, TreeSelfCheck
from services.ava_root.wiring import WiringContext, WiringParticipant
from shared.config import settings
from shared.platform import IS_MACOS

STATIC_PROBES: Mapping[str, str] = {}
"""Spec-less unit id -> `"module:attribute"` probe reference.

v1 is empty on purpose: the host-policy / data-plane / native-stack class is
registered here when the wiring slice (W1.3) names its probe modules; the
reference is validated eagerly and imported lazily (the `register_ref`
contract).
"""

HELPER_PROBE_UNIT_ID = "permissions-helper"
"""The launchd-owned permissions helper's unit id in the health roster (the
name the watchdog era also uses)."""

HELPER_PROBE_REF = "services.healthchecks.permissions_helper:probe"
"""The helper's total verdict probe (task #3393): reads the launchd job state,
classifies an LWCR-stuck job, and never acts. Repair stays with the helper's
own lifecycle (CLI converge / the era-1 loop) — this entry detects and the
health monitor escalates; it is not a revival path."""


def build_wiring(context: WiringContext) -> list[WiringParticipant]:
    """The reference participant set: health monitor + tree self-check."""
    registry = ProbeRegistry()
    registry.register_specs(build_services())
    for unit_id, ref in STATIC_PROBES.items():
        registry.register_ref(unit_id, ref)
    if _helper_probe_enabled():
        registry.register_ref(HELPER_PROBE_UNIT_ID, HELPER_PROBE_REF)
    return assemble(context, registry)


def _helper_probe_enabled() -> bool:
    """A launchd helper job worth watching exists only where it was enabled."""
    return IS_MACOS and settings.services.permissions_helper_enabled


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
