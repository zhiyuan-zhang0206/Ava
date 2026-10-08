"""Bind actual root units to readiness, diagnostics, and tree self-check.

Service recovery belongs to HealthMonitor; host diagnostics have no launch
verbs. One round coordinator publishes freshness after both parts complete.
Native data-plane custody and the macOS parent helper remain explicit external
boundaries; diagnostics never repair them from inside the service subtree.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType

from base.deploy.progress_timeout import (
    CRITICAL_SERVICE_SESSIONS,
    NON_CRITICAL_SERVICE_READY_TIMEOUT_S,
    SERVICE_READY_TIMEOUT_S,
)
from ops.roster import build_services
from services.supervision.ava_root.health import HealthConfig, HealthMonitor
from services.supervision.ava_root.probes import ProbeError, ProbeRegistry
from services.supervision.ava_root.selfcheck import SelfCheckConfig, TreeSelfCheck
from services.supervision.ava_root.wiring import WiringContext, WiringParticipant
from services.supervision.ava_root_glue.diagnostic_probes import build_diagnostics
from services.supervision.ava_root_glue.diagnostics import (
    Diagnostic,
    DiagnosticMonitor,
    RootHealthRounds,
)

STATIC_PROBES: Mapping[str, str] = MappingProxyType({})
"""Spec-less unit id -> `"module:attribute"` probe reference.

v1 is empty on purpose: the host-policy / data-plane / native-stack class is
registered here when the wiring slice (W1.3) names its probe modules; the
reference is validated eagerly and imported lazily (the `register_ref`
contract).
"""


def build_wiring(context: WiringContext) -> list[WiringParticipant]:
    """The reference participant set: health monitor + tree self-check."""
    from base.deploy.release.runtime_interpreter import capture_loaded_runtime
    from base.paths import ava_home

    home = ava_home().resolve(strict=True)
    identity = capture_loaded_runtime()
    if Path.cwd().resolve() != Path(identity.cwd):
        raise RuntimeError("root cwd differs from its loaded runtime")
    context.supervisor.bind_runtime(identity, home=home)
    registry = ProbeRegistry()
    requested = {unit.id for unit in context.registry.units}
    registry.register_specs(spec for spec in build_services() if spec.session in requested)
    for unit_id, ref in STATIC_PROBES.items():
        registry.register_ref(unit_id, ref)
    missing = requested - set(registry.unit_ids())
    if missing:
        raise ProbeError(f"root units lack readiness probes: {', '.join(sorted(missing))}")
    startup_graces = {
        unit_id: SERVICE_READY_TIMEOUT_S
        if unit_id in CRITICAL_SERVICE_SESSIONS
        else NON_CRITICAL_SERVICE_READY_TIMEOUT_S
        for unit_id in requested
    }
    return assemble(
        context,
        registry,
        diagnostics=build_diagnostics(requested),
        startup_graces=startup_graces,
    )


def assemble(
    context: WiringContext,
    registry: ProbeRegistry,
    *,
    health_config: HealthConfig | None = None,
    selfcheck_config: SelfCheckConfig | None = None,
    diagnostics: Sequence[Diagnostic] | None = None,
    startup_graces: Mapping[str, float] | None = None,
) -> list[WiringParticipant]:
    """Build the two monitors over `registry` and attach their status surfaces."""
    monitor = HealthMonitor(
        context.supervisor,
        registry,
        config=health_config,
        tasks=context.participant_tasks,
        startup_graces=startup_graces,
    )
    check = TreeSelfCheck(
        context.supervisor, config=selfcheck_config, tasks=context.participant_tasks
    )
    if diagnostics is not None:
        rounds = RootHealthRounds(
            monitor,
            DiagnosticMonitor(diagnostics),
            interval_s=60 if health_config is None else health_config.interval_s,
            tasks=context.participant_tasks,
        )
        context.supervisor.attach_health(rounds)
        context.supervisor.attach_metrics(check)
        return [rounds, check]
    context.supervisor.attach_health(monitor)
    context.supervisor.attach_metrics(check)
    return [monitor, check]
