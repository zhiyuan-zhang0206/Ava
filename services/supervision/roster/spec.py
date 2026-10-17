"""Service spec — capability filtering and gate policy for machine desired state.

Given a host's capability set, this K8s-shaped module family answers what should
run. ``services.supervision.roster.build_services()`` is the canonical roster; this module applies
capability selection and runtime gates to it.

Every service declares its ``ServiceSpec.capabilities`` in one of three groups:
gateway-only, agent-runner-only, or both. ``services_for_capabilities(roles)``
selects services whose capabilities intersect the host's roles. A service also
declares ``requires_db`` so database-dependent readiness remains explicit.

Plugins expose ``services() -> tuple[ServiceSpec, ...]`` from their services
module. ``plugin_services()`` discovers code-present plugins and appends them
to the roster: plugin declares, the roster discovers. Each plugin service's
own ``ServiceSpec.gate`` keeps cluster-level enablement out of ``gate_reason``.
The fleet task daemon follows this path; see
``docs/decisions/extensions/plugins/2026-07-19-plugin-registered-services.md``.

Layer: this module family lives in ``services/supervision``; it imports ``base``,
plus lazy function-local reaches into the shared-tier browser identity probe and
gate app-port source. Nothing reaches up into cli/gateway, so start, root
monitoring, and ``meiva status`` share one roster.

Native Postgres, Redis, and PgBouncer have separate data-plane custody so they
can remain available during an application-root transition. The macOS helper
is root's platform parent, not an application service. Read-only extra checks
live in the root diagnostic roster; they never acquire service ownership.
The definitions live in ``services.supervision.roster.service_spec``,
``services.supervision.roster``, and ``services.supervision.roster.spec``;
callers import them from there.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from base.cluster.dataplane.redis_bridge import RedisBridgeUnconfiguredError, redis_bridge_endpoint
from base.cluster.machine import MachineIdentity, MachineRoles
from base.config import settings
from base.host.system.hostport import Host
from base.host.system.probes import (
    browser_incapability,
    permissions_helper_incapability,
)
from base.log import logger
from base.paths import meiva_home
from base.telemetry.observability import collector_allowed_for_home, gateway_observability_home
from services.supervision.roster import build_services
from services.supervision.roster.plugin_services import PluginServiceError
from services.supervision.roster.service_spec import (
    ServiceSpec as ServiceSpec,  # re-export: generated plugin fixtures
)


def _computer_mcp_gate_reason(*, host: Host) -> str | None:
    """Why the computer-mcp service is gated out, or None when it should run.

    Platform gate only: the daemon needs the permissions helper (the single
    TCC grant-holder it executes through). There is no governance gate —
    per-agent permission division is a prompt-level peer convention, not
    code-enforced (user ruling 2026-08-10).
    """
    if not settings.services.permissions_helper_enabled:
        return "disabled (MEIVA_PERMISSIONS_HELPER_ENABLED off)"
    if permissions_helper_incapability(host=host) is not None:
        return permissions_helper_incapability(host=host)
    return None


def _redis_bridge_gate_reason(*, machine: MachineIdentity) -> str | None:
    """Why redis-bridge is gated out, or None when this host needs the relay.

    The relay exists only on a macOS gateway with a local data plane and a declared
    non-loopback address; the same answer drives `meiva status` and the cluster health probe.
    """
    try:
        endpoint = redis_bridge_endpoint(
            meiva_home(),
            machine_host=machine.host,
            remote_data_plane=lambda: settings.data_plane.is_remote,
        )
    except RedisBridgeUnconfiguredError as exc:
        return str(exc)
    return None if endpoint is not None else "no off-box Redis ingress on this host"


def _otel_collector_gate_reason(*, machine: MachineIdentity) -> str | None:
    """Why otel-collector is gated out, or None when it should run.

    This is the roster sibling of ``ensure_otel_collector_step`` in
    ``cli/commands/observability/otel_collector.py`` and ``_collector_serves_this_home`` in
    ``services/supervision/healthchecks/otel_collector.py``. All three share
    ``collector_allowed_for_home`` (marker OR station capability OR explicit
    ``MEIVA_TELEMETRY_OTLP_ENDPOINT`` override) so the roster, ``meiva start``,
    ``meiva status``, the root, rollout readiness, and cluster health probe agree
    about which gateway owns the collector. Pure agent-runners retain their
    relay collector.
    """
    home = meiva_home()
    if not collector_allowed_for_home(
        gateway_observability_home(machine, home=home), machine=machine, unit_home=home
    ):
        return (
            "this gateway home is not the observability station (lgtm-host "
            "marker absent, no observability-station capability); telemetry "
            "export is unavailable; set MEIVA_TELEMETRY_OTLP_ENDPOINT to use an "
            "explicit collector"
        )
    return None


# Uniform "session X is gated out when its single settings flag is off" rows —
# each entry's predicate is checked in one small loop by `_flag_gate_reason`
# instead of a repeated `if session == NAME and not settings...: return ...`
# chain, which is what previously drove `gate_reason`'s complexity above the
# hard ceiling. Every row here has exactly this shape; a session whose gate
# needs more than one flag or a non-boolean comparison (browser, mcp-daemon,
# computer-mcp, the lgtm trio, otel-collector) stays a dedicated branch
# in `_core_gate_reason` below.
_FLAG_GATES: tuple[tuple[str, Callable[[], bool], str], ...] = (
    (
        "heartbeat",
        lambda: settings.daemon.heartbeat_enabled,
        "disabled (MEIVA_HEARTBEAT_ENABLED off)",
    ),
    (
        "delivery-watchdog",
        lambda: settings.daemon.delivery_watchdog_enabled,
        "disabled (MEIVA_DELIVERY_WATCHDOG_ENABLED off)",
    ),
    (
        "im-bridge",
        lambda: settings.services.im_bridge_enabled,
        "disabled (MEIVA_IM_BRIDGE_ENABLED off)",
    ),
)


def _flag_gate_reason(session: str) -> str | None:
    """The reason for a session in `_FLAG_GATES`, or None (enabled / not one of these)."""
    for name, enabled, reason in _FLAG_GATES:
        if session == name and not enabled():
            return reason
    return None


def _browser_family_gate_reason(*, host: Host) -> str | None:
    if not settings.services.browser_enabled:
        return "disabled (MEIVA_BROWSER_ENABLED off)"
    return browser_incapability(
        settings.services.chrome_binary, host=host
    )  # display / Chrome / npx, or None when capable


def _core_gate_reason(session: str, *, machine: MachineIdentity, host: Host) -> str | None:
    """The session-name-keyed half of `gate_reason` — every core service
    without its own plugin-registered ``gate``."""
    if session in ("browser", "browser-mcp"):
        return _browser_family_gate_reason(host=host)
    if session == "computer-mcp":
        return _computer_mcp_gate_reason(host=host)
    if session in {"loki", "prometheus", "grafana"}:
        from services.supervision.healthchecks.lgtm import is_lgtm_host

        return (
            None if is_lgtm_host(machine=machine) else "this home is not an observability station"
        )
    if session == "otel-collector":
        return _otel_collector_gate_reason(machine=machine)
    if session == "redis-bridge":
        return _redis_bridge_gate_reason(machine=machine)
    return _flag_gate_reason(session)


def _plugin_gate_reason(spec: ServiceSpec) -> str | None:
    """A plugin gate must supply its declared reason contract."""
    if spec.gate is None:
        raise PluginServiceError(f"service {spec.session!r} has no plugin gate")
    reason = spec.gate()
    if reason is not None and not isinstance(reason, str):
        raise PluginServiceError(f"gate for {spec.session!r} must return str or None")
    return reason


def gate_reason(spec: ServiceSpec, *, machine: MachineIdentity, host: Host) -> str | None:
    """Why a service is config/capability-gated OUT of the start roster, or None if
    it will run. The single place the gate's *reason* is computed, so the start
    path (drops it), ``meiva start`` (logs it), and ``meiva status`` (shows it) stay
    consistent.

    A service that carries its own ``gate`` (plugin-registered services) is asked
    directly — its fleet/plugin-domain toggle lives with the plugin, not here.
    Core services are gated by session name in `_core_gate_reason`.
    """
    if spec.gate is not None:
        try:
            return _plugin_gate_reason(spec)
        except Exception:
            logger.opt(exception=True).error(
                "gate for {} failed; roster evaluation aborted", spec.session
            )
            raise
    return _core_gate_reason(spec.session, machine=machine, host=host)


def services_for_capabilities_annotated(
    roles: MachineRoles, *, machine: MachineIdentity, host: Host
) -> tuple[tuple[ServiceSpec, str | None], ...]:
    """The union of services this host's capability set runs, each paired with its
    gate reason (None = will start, a string = gated out + why).

    A service is included iff its ``capabilities`` intersect ``roles`` — so a
    gateway host gets the gateway-group services, an agent-runner host the
    agent-runner-group, and a single box the union. Iterates ``build_services()``
    in authored order, so each single-capability view keeps its load-bearing order.

    Config/capability-gated services (browser, disabled heartbeat; and
    plugin services with their own gate, e.g. disabled task-maintenance) are kept
    in this list WITH their reason rather than dropped, so ``meiva start`` /
    ``meiva status`` can show WHY a service is absent instead of silently shrinking
    the roster. ``services_for_capabilities`` is the start-roster view that drops
    them.
    """
    return tuple(
        (s, gate_reason(s, machine=machine, host=host))
        for s in build_services()
        if s.capabilities & roles
    )


def services_for_capabilities(
    roles: MachineRoles, *, machine: MachineIdentity, host: Host
) -> tuple[ServiceSpec, ...]:
    """The start roster: the capability-union services that will actually launch
    (config/capability-gated ones dropped). ``services_for_capabilities_annotated``
    is the diagnostic view that keeps the gated-out services + their reason."""
    return tuple(
        s
        for s, reason in services_for_capabilities_annotated(roles, machine=machine, host=host)
        if reason is None
    )


@dataclass(frozen=True)
class Spec:
    """A host's desired state — the roster it should run and (as it converges
    here) the data plane it should run against.

    Slice 1 fills in the service roster. The data-plane desired state (per-cluster instance / ports / bind / redis-ACL
    users) lands after the data-plane retirement PR, whose model it will read from
    rather than re-derive. See ``future/infra/ops-module.md`` for the full
    Spec content and the batch sequence.
    """

    roles: MachineRoles
    machine: MachineIdentity
    host: Host

    def services(self) -> tuple[ServiceSpec, ...]:
        """The start roster for this host's capabilities (gated-out dropped)."""
        return services_for_capabilities(self.roles, machine=self.machine, host=self.host)

    def services_annotated(self) -> tuple[tuple[ServiceSpec, str | None], ...]:
        """The diagnostic roster — every capability-matched service + its gate reason."""
        return services_for_capabilities_annotated(self.roles, machine=self.machine, host=self.host)
