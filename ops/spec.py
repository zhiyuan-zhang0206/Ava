"""Ops Spec — capability filtering and gate policy for machine desired state.

Given a host's capability set, this K8s-shaped module family answers what should
run. ``ops.roster.build_services()`` is the canonical roster; this module applies
capability selection and runtime gates to it.

Every service declares its ``ServiceSpec.capabilities`` in one of three groups:
gateway-only, agent-runner-only, or both. ``services_for_capabilities(roles)``
selects services whose capabilities intersect the host's roles. A service also
declares ``requires_db`` so database-dependent readiness remains explicit.

Plugins expose ``services() -> tuple[ServiceSpec, ...]`` from their services
module. ``plugin_services()`` discovers code-present plugins and appends them
to the roster: plugin declares, ops discovers. Each plugin service's
own ``ServiceSpec.gate`` keeps cluster-level enablement out of ``_gate_reason``.
The fleet task daemon follows this path; see
``decisions/2026-07-19-plugin-registered-services.md``.

Layer: the ``ops`` module family imports ``base``, plus lazy function-local
reaches into the shared-tier browser identity probe and gate app-port source.
Nothing reaches up into cli/gateway, so start, root monitoring, and ``ava status``
share one roster.

Native Postgres, Redis, and PgBouncer have separate data-plane custody so they
can remain available during an application-root transition. The macOS helper
is root's platform parent, not an application service. Read-only extra checks
live in the root diagnostic roster; they never acquire service ownership.
``cli.commands._repo`` re-exports ``ServiceSpec`` / ``build_services`` /
``services_for_capabilities`` under their historical names as a cli-facing façade
(so existing `from cli.commands._repo import ...` call sites keep working), but the
definitions live in ``ops.roster.service_spec``, ``ops.roster``, and ``ops.spec``.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from base.cluster.machine import MachineRoles
from base.config import settings
from base.host.system.probes import (
    browser_incapability,
    browser_mcp_incapability,
    permissions_helper_incapability,
    unix_sockets_available,
)
from base.log import logger
from base.telemetry.observability import collector_allowed_for_home, gateway_observability_home
from ops.roster.service_spec import (
    ServiceSpec as ServiceSpec,  # re-export: generated plugin fixtures
)


def plugin_services() -> tuple[ServiceSpec, ...]:
    """The services contributed by the plugins PRESENT on this machine.

    Discovery, not import-of-known-plugins: `base.packages.plugins.enable_config` enumerates the
    plugins installed on THIS machine (builtin + external), and each that ships a
    ``services.py`` exposing ``services() -> tuple[ServiceSpec, ...]`` gets folded
    into the roster. This keeps the direction "plugin declares, ops discovers" — no
    reverse edge from ops into any specific plugin's domain code.

    Discovery keys on plugin **presence**, NOT the agent-facing enable-state
    (``ava plugins enable/disable``): the roster is a machine/cluster concern, and
    coupling it to the agent-plugin-registration plane would be cross-plane
    semantics. A plugin gates its own service (whether it starts) via an explicit
    settings field in ``ServiceSpec.gate`` — e.g. task-maintenance's
    ``AVA_TASK_MAINTENANCE_ENABLED`` — which is deterministic at daemon-start and
    unaffected by any per-agent config overlay. start / root / status all
    follow, since they derive from `build_services()`.

    The ``services.py`` module is loaded by FILE PATH (like
    `base.packages.plugins.enable_config.update_all_disk_images` loads `default_config.py`) so an
    external plugin under ``~/.ava/plugins/`` — off the ``plugins.`` package path —
    can register too; it must import only light deps (ops / base), never its
    own `plugin.py`, so this load does not drag the agent kernel into the ops
    process.

    Fail-soft per plugin (user ruling 2026-09-11): a ``services.py`` that fails
    to load, a file without a ``services()`` function, or a ``services()`` call
    that raises is skipped with a loud report
    (``base.packages.plugins.load_report``) — one broken plugin must not block
    `ava start` / the root roster for every other plugin. The session-name
    collision guard stays fail-closed: no rule can pick a winner between two
    owners of one session name.
    """
    from base.packages.plugins import load_report
    from base.packages.plugins.enable_config import installed_plugin_dirs

    specs: list[ServiceSpec] = []
    for name, plugin_dir in sorted(installed_plugin_dirs().items()):
        services_py = plugin_dir / "services.py"
        if not services_py.exists():
            continue
        try:
            module = _load_plugin_module(name, services_py)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:
            load_report.report_plugin_load_failure(name, exc)
            continue
        declare = getattr(module, "services", None)
        if declare is None:
            load_report.report_plugin_load_failure(
                name,
                PluginServiceError(
                    f"plugin {name!r} ships a services.py but it defines no `services()` function"
                ),
            )
            continue
        try:
            specs.extend(declare())
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:
            load_report.report_plugin_load_failure(name, exc)
    return tuple(specs)


def _load_plugin_module(name: str, services_py: Path) -> object:
    """Load and register ``services.py`` so its own healthcheck is importable.

    An external plugin can keep its protocol health probes here and declare
    ``healthcheck_module=__name__``. Root health monitoring must resolve that
    module even when no agent has bootstrapped the external plugin namespace. Registration
    before execution also gives dataclasses their normal import-time identity.
    Failed imports restore the prior module, never a half-executed replacement.
    """
    spec = importlib.util.spec_from_file_location(f"plugins.{name}.services", services_py)
    if spec is None or spec.loader is None:
        raise PluginServiceError(f"cannot load services.py for plugin {name!r} ({services_py})")
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(spec.name)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        if previous is None:
            sys.modules.pop(spec.name, None)
        else:
            sys.modules[spec.name] = previous
        raise
    return module


def _assert_unique_sessions(core: tuple[ServiceSpec, ...], plugin: tuple[ServiceSpec, ...]) -> None:
    """Fail fast if a plugin service's session name collides with a core service or
    another plugin's — the roster is keyed on `session` (session, root
    roster, status), so a duplicate would silently shadow one entry."""
    seen = {s.session for s in core}
    for s in plugin:
        if s.session in seen:
            raise PluginServiceError(
                f"plugin service session {s.session!r} collides with an existing service; "
                "session names must be unique across core + all plugins"
            )
        seen.add(s.session)


class PluginServiceError(RuntimeError):
    """A plugin's service declaration is malformed or collides with another."""


def _computer_mcp_gate_reason() -> str | None:
    """Why the computer-mcp service is gated out, or None when it should run.

    Platform gates only: the daemon needs the permissions helper (the single
    TCC grant-holder it executes through), the AF_UNIX transport its socket
    protocol uses. There is no governance gate — per-agent permission
    division is a prompt-level peer convention, not code-enforced (user ruling
    2026-08-10).
    """
    if not settings.services.permissions_helper_enabled:
        return "disabled (AVA_PERMISSIONS_HELPER_ENABLED off)"
    if permissions_helper_incapability() is not None:
        return permissions_helper_incapability()
    if not unix_sockets_available():
        return "no AF_UNIX sockets (computer-mcp's transport is POSIX-only)"
    return None


def _otel_collector_gate_reason() -> str | None:
    """Why otel-collector is gated out, or None when it should run.

    This is the roster sibling of ``ensure_otel_collector_step`` in
    ``cli/commands/observability/otel_collector.py`` and ``_collector_serves_this_home`` in
    ``services/healthchecks/otel_collector.py``. All three share
    ``collector_allowed_for_home`` (marker OR station capability OR explicit
    ``AVA_TELEMETRY_OTLP_ENDPOINT`` override) so the roster, ``ava start``,
    ``ava status``, the root, rollout readiness, and cluster health probe agree
    about which gateway owns the collector. Pure agent-runners retain their
    relay collector.
    """
    if not collector_allowed_for_home(gateway_observability_home()):
        return (
            "this gateway home is not the observability station (lgtm-host "
            "marker absent, no observability-station capability); telemetry "
            "export is unavailable; set AVA_TELEMETRY_OTLP_ENDPOINT to use an "
            "explicit collector"
        )
    return None


# Uniform "session X is gated out when its single settings flag is off" rows —
# each entry's predicate is checked in one small loop by `_flag_gate_reason`
# instead of a repeated `if session == NAME and not settings...: return ...`
# chain, which is what previously drove `_gate_reason`'s complexity above the
# hard ceiling. Every row here has exactly this shape; a session whose gate
# needs more than one flag or a non-boolean comparison (browser, mcp-daemon,
# computer-mcp, the lgtm trio, otel-collector) stays a dedicated branch
# in `_core_gate_reason` below.
_FLAG_GATES: tuple[tuple[str, Callable[[], bool], str], ...] = (
    (
        "heartbeat",
        lambda: settings.daemon.heartbeat_enabled,
        "disabled (AVA_HEARTBEAT_ENABLED off)",
    ),
    (
        "delivery-watchdog",
        lambda: settings.daemon.delivery_watchdog_enabled,
        "disabled (AVA_DELIVERY_WATCHDOG_ENABLED off)",
    ),
    (
        "im-bridge",
        lambda: settings.services.im_bridge_enabled,
        "disabled (AVA_IM_BRIDGE_ENABLED off)",
    ),
)


def _flag_gate_reason(session: str) -> str | None:
    """The reason for a session in `_FLAG_GATES`, or None (enabled / not one of these)."""
    for name, enabled, reason in _FLAG_GATES:
        if session == name and not enabled():
            return reason
    return None


def _browser_family_gate_reason(session: str) -> str | None:
    if not settings.services.browser_enabled:
        return "disabled (AVA_BROWSER_ENABLED off)"
    # Two services, two capability probes: browser-mcp needs a strict SUPERSET
    # of what the headed browser needs (the same display / Chrome / npx prongs
    # plus an AF_UNIX transport), so a host can legitimately run `browser` and
    # not `browser-mcp` — which is exactly a Windows agent-runner. Sharing one
    # probe put browser-mcp in that host's start roster with no skip
    # annotation, and it failed every launch.
    if session == "browser-mcp":
        return browser_mcp_incapability()
    return browser_incapability()  # display / Chrome / npx, or None when capable


def _core_gate_reason(session: str) -> str | None:
    """The session-name-keyed half of `_gate_reason` — every core service
    without its own plugin-registered ``gate``."""
    if session in ("browser", "browser-mcp"):
        return _browser_family_gate_reason(session)
    if session == "mcp-daemon" and not unix_sockets_available():
        # Same transport story as browser-mcp: the daemon binds a Unix socket
        # (ava/mcps/_daemon.py) and its healthcheck dials it, so without AF_UNIX
        # the service can never start and the root would judge it dead every
        # round and log a restart failure — a Windows agent-runner, exactly.
        return "no AF_UNIX sockets (mcp-daemon's transport is POSIX-only)"
    if session == "pty-sessions" and not unix_sockets_available():
        # The service binds a Unix socket and forks ptys: a host without them
        # (a Windows unit, which runs no shells) cannot start it.
        return "no AF_UNIX sockets (pty-sessions' transport is POSIX-only)"
    if session == "computer-mcp":
        return _computer_mcp_gate_reason()
    if session in {"loki", "prometheus", "grafana"}:
        from services.healthchecks.lgtm import is_lgtm_host

        return None if is_lgtm_host() else "this home is not an observability station"
    if session == "otel-collector":
        return _otel_collector_gate_reason()
    return _flag_gate_reason(session)


def _gate_reason(spec: ServiceSpec) -> str | None:
    """Why a service is config/capability-gated OUT of the start roster, or None if
    it will run. The single place the gate's *reason* is computed, so the start
    path (drops it), ``ava start`` (logs it), and ``ava status`` (shows it) stay
    consistent.

    A service that carries its own ``gate`` (plugin-registered services) is asked
    directly — its fleet/plugin-domain toggle lives with the plugin, not here.
    Core services are gated by session name in `_core_gate_reason`.
    """
    if spec.gate is not None:
        try:
            return spec.gate()
        except Exception as exc:  # a broken gate must not kill the watchdog
            # 2026-08-08 incident: the memory-indexer gate read the 'agent'
            # config domain from the gateway watchdog's process profile and
            # raised AttributeError, killing the whole watchdog on its first
            # tick (no healthchecks ran until a respawn without the profile).
            # Fail OPEN — run the service — and log, so one plugin's gate bug
            # can never take the supervisor down; the capability filter above
            # already scoped the service to this host's role.
            logger.warning("gate for {} raised (failing open): {}", spec.session, exc)
            return None
    return _core_gate_reason(spec.session)


def services_for_capabilities_annotated(
    roles: MachineRoles,
) -> tuple[tuple[ServiceSpec, str | None], ...]:
    """The union of services this host's capability set runs, each paired with its
    gate reason (None = will start, a string = gated out + why).

    A service is included iff its ``capabilities`` intersect ``roles`` — so a
    gateway host gets the gateway-group services, an agent-runner host the
    agent-runner-group, and a single box the union. Iterates ``build_services()``
    in authored order, so each single-capability view keeps its load-bearing order.

    Config/capability-gated services (browser, disabled heartbeat; and
    plugin services with their own gate, e.g. disabled task-maintenance) are kept
    in this list WITH their reason rather than dropped, so ``ava start`` /
    ``ava status`` can show WHY a service is absent instead of silently shrinking
    the roster. ``services_for_capabilities`` is the start-roster view that drops
    them.
    """
    return tuple((s, _gate_reason(s)) for s in build_services() if s.capabilities & roles)


def services_for_capabilities(roles: MachineRoles) -> tuple[ServiceSpec, ...]:
    """The start roster: the capability-union services that will actually launch
    (config/capability-gated ones dropped). ``services_for_capabilities_annotated``
    is the diagnostic view that keeps the gated-out services + their reason."""
    return tuple(s for s, reason in services_for_capabilities_annotated(roles) if reason is None)


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

    def services(self) -> tuple[ServiceSpec, ...]:
        """The start roster for this host's capabilities (gated-out dropped)."""
        return services_for_capabilities(self.roles)

    def services_annotated(self) -> tuple[tuple[ServiceSpec, str | None], ...]:
        """The diagnostic roster — every capability-matched service + its gate reason."""
        return services_for_capabilities_annotated(self.roles)


# Compatibility re-export: legacy importers (`from ops.spec import
# build_services`, tests) take the canonical roster from this module.
# Placed at the BOTTOM deliberately: roster's build_services calls back into
# this module's helpers (plugin_services /
# _assert_unique_sessions) lazily, so this edge must not run while this module
# is partially initialized (spec → roster at the top would be a load-time
# edge in the opposite direction of the call-time edge — keep both lazy).
from ops.roster import build_services as build_services  # noqa: E402 — deliberate bottom re-export
