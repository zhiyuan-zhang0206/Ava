"""Ops Spec — capability filtering and gate policy for machine desired state.

Given a host's capability set, this K8s-shaped module family answers what should
run. ``ops.roster.build_services()`` is the canonical roster; this module applies
capability selection and runtime gates to it.

Every service declares its ``ServiceSpec.capabilities`` in one of three groups:
gateway-only, agent-runner-only, or both. ``services_for_capabilities(roles)``
selects services whose capabilities intersect the host's roles. A service also
declares ``requires_db`` so the watchdog can hold back exactly the database's
users during a DB-scoped round block (``ops.controllers.base.BlockScope``).

Plugins expose ``services() -> tuple[ServiceSpec, ...]`` from their services
module. ``_plugin_services()`` discovers code-present plugins and appends them
to the roster: plugin declares, ops discovers. Each plugin service's
own ``ServiceSpec.gate`` keeps cluster-level enablement out of ``_gate_reason``.
The fleet task daemon follows this path; see
``decisions/2026-07-19-plugin-registered-services.md``.

Layer: the ``ops`` module family imports ``shared``, plus lazy function-local
reaches into the shared-tier browser identity probe and gate app-port source.
Nothing reaches up into cli/gateway, so start, watchdog, and ``ava status``
share one roster.

**Deliberately outside the roster** (each documented at its own site): the
``gate`` entry-port service (launchd KeepAlive / pidfile job, no session row —
``ops/controllers/_converge_gate.py``, probed via ``probe_gate``, not the
watchdog), the OS-level watchdog-probe jobs (``shared/os_watchdog_probe.py``),
and the watchdog's hand-prepended ``redis-acl`` healthcheck
(``services/watchdog/daemon.py``). These are not sessions, so
``build_services()`` does not see them by design.
``cli.commands._repo`` re-exports ``ServiceSpec`` / ``build_services`` /
``services_for_capabilities`` under their historical names as a cli-facing façade
(so existing `from cli.commands._repo import ...` call sites keep working), but the
definitions live in ``service_spec.py``, ``roster.py``, and ``spec.py``.
"""

from __future__ import annotations

import importlib.util
import shlex
from dataclasses import dataclass, replace
from pathlib import Path

from ops.service_spec import ServiceSpec as ServiceSpec  # re-export: generated plugin fixtures
from shared.config import settings
from shared.log import logger
from shared.machine import MachineRoles
from shared.observability import collector_allowed_for_home, gateway_observability_home
from shared.platform import IS_WINDOWS
from shared.platform_probes import (
    browser_incapability,
    browser_mcp_incapability,
    permissions_helper_incapability,
    unix_sockets_available,
)


def _bind_runtime_command(spec: ServiceSpec) -> ServiceSpec:
    """Bind Python services to the loaded runtime without changing their gates."""
    from shared.runtime_interpreter import WHEEL_RUNTIME, runtime_python

    prefix = ".venv/bin/python "
    if not WHEEL_RUNTIME or not spec.cmd.startswith(prefix):
        return spec
    return replace(
        spec, cmd=f"{shlex.quote(str(runtime_python()))} -I -B -X utf8 {spec.cmd[len(prefix) :]}"
    )


def _plugin_services() -> tuple[ServiceSpec, ...]:
    """The services contributed by the plugins PRESENT on this machine.

    Discovery, not import-of-known-plugins: `shared.plugins_config` enumerates the
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
    unaffected by any per-agent config overlay. start / watchdog / status all
    follow, since they derive from `build_services()`.

    The ``services.py`` module is loaded by FILE PATH (like
    `shared.plugins_config.update_all_disk_images` loads `default_config.py`) so an
    external plugin under ``~/.ava/plugins/`` — off the ``plugins.`` package path —
    can register too; it must import only light deps (ops / shared), never its
    own `plugin.py`, so this load does not drag the agent kernel into the ops
    process.
    """
    from shared.plugins_config import installed_plugin_dirs

    specs: list[ServiceSpec] = []
    for name, plugin_dir in sorted(installed_plugin_dirs().items()):
        services_py = plugin_dir / "services.py"
        if not services_py.exists():
            continue
        module = _load_plugin_module(name, services_py)
        declare = getattr(module, "services", None)
        if declare is None:
            raise PluginServiceError(
                f"plugin {name!r} ships a services.py but it defines no `services()` function"
            )
        specs.extend(declare())
    return tuple(specs)


def _load_plugin_module(name: str, services_py: Path) -> object:
    """Import a plugin's ``services.py`` by file path, returning the module."""
    spec = importlib.util.spec_from_file_location(f"plugins.{name}.services", services_py)
    if spec is None or spec.loader is None:
        raise PluginServiceError(f"cannot load services.py for plugin {name!r} ({services_py})")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _assert_unique_sessions(core: tuple[ServiceSpec, ...], plugin: tuple[ServiceSpec, ...]) -> None:
    """Fail fast if a plugin service's session name collides with a core service or
    another plugin's — the roster is keyed on `session` (session, watchdog
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
    protocol uses, and a non-Windows host (Windows computer-use is the phase-3
    pilot, task #1101). There is no governance gate — per-agent permission
    division is a prompt-level peer convention, not code-enforced (user ruling
    2026-08-10).
    """
    if not settings.services.permissions_helper_enabled:
        return "disabled (AVA_PERMISSIONS_HELPER_ENABLED off)"
    if permissions_helper_incapability() is not None:
        return permissions_helper_incapability()
    if not unix_sockets_available():
        return "no AF_UNIX sockets (computer-mcp's transport is POSIX-only)"
    if IS_WINDOWS:
        # The Windows C# helper lacks screen_size/frontmost_app (the snapshot
        # geometry needs them); Windows is the phase-3 pilot (task #1101) —
        # enable it there with the helper methods added.
        return "Windows computer-use is a phase-3 pilot (task #1101)"
    return None


def _otel_collector_gate_reason() -> str | None:
    """Why otel-collector is gated out, or None when it should run.

    This is the roster sibling of ``ensure_otel_collector_step`` in
    ``cli/commands/_otel_collector.py`` and ``_collector_serves_this_home`` in
    ``services/healthchecks/otel_collector.py``. All three share
    ``collector_allowed_for_home`` (marker OR station capability OR explicit
    ``AVA_TELEMETRY_OTLP_ENDPOINT`` override) so the roster, ``ava start``,
    ``ava status``, watchdog, rollout readiness, and cluster health probe agree
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


def _gate_reason(spec: ServiceSpec) -> str | None:
    """Why a service is config/capability-gated OUT of the start roster, or None if
    it will run. The single place the gate's *reason* is computed, so the start
    path (drops it), ``ava start`` (logs it), and ``ava status`` (shows it) stay
    consistent.

    A service that carries its own ``gate`` (plugin-registered services) is asked
    directly — its fleet/plugin-domain toggle lives with the plugin, not here.
    Core services are gated by session name below.
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
            logger.warning("gate for %s raised (failing open): %s", spec.session, exc)
            return None
    session = spec.session
    if session in ("browser", "browser-mcp"):
        if not settings.services.browser_enabled:
            return "disabled (AVA_BROWSER_ENABLED off)"
        # Two services, two capability probes: browser-mcp needs a strict
        # SUPERSET of what the headed browser needs (the same display / Chrome /
        # npx prongs plus an AF_UNIX transport), so a host can legitimately run
        # `browser` and not `browser-mcp` — which is exactly a Windows
        # agent-runner. Sharing one probe put browser-mcp in that host's start
        # roster with no skip annotation, and it failed every launch.
        if session == "browser-mcp":
            return browser_mcp_incapability()
        return browser_incapability()  # display / Chrome / npx, or None when capable
    if session == "mcp-daemon" and not unix_sockets_available():
        # Same transport story as browser-mcp: the daemon binds a Unix socket
        # (ava/_mcps_daemon.py) and its healthcheck dials it, so without AF_UNIX
        # the service can never start and the watchdog would judge it dead every
        # 60s and log a restart failure — a Windows agent-runner, exactly.
        return "no AF_UNIX sockets (mcp-daemon's transport is POSIX-only)"
    if session == "computer-mcp":
        return _computer_mcp_gate_reason()
    if session == "otel-collector":
        return _otel_collector_gate_reason()
    if session == "heartbeat" and not settings.daemon.heartbeat_enabled:
        return "disabled (AVA_HEARTBEAT_ENABLED off)"
    if session == "delivery-watchdog" and not settings.daemon.delivery_watchdog_enabled:
        return "disabled (AVA_DELIVERY_WATCHDOG_ENABLED off)"
    if session == "pitr-uploader" and not settings.physical_backup.pitr_enabled:
        return "disabled (AVA_PITR_ENABLED off)"
    if session == "pitr-base-candidate" and not settings.physical_backup.pitr_base_backup_enabled:
        return "disabled (AVA_PITR_BASE_BACKUP_ENABLED off)"
    if session == "im-bridge" and not settings.services.im_bridge_enabled:
        return "disabled (AVA_IM_BRIDGE_ENABLED off)"
    if session == "milvus" and settings.services.memory_search_backend != "milvus":
        # The milvus-lite server only serves the memory indexer's milvus
        # backend; numpy (default) and pgvector never dial it. Without the
        # gate every `ava start` launched an idle ~1GB milvus-lite process the
        # memory search never uses (2026-09-02 numpy-default ruling; daemon
        # was disabled by hand 2026-09-03, this gate makes it durable).
        return (
            "memory-search backend is "
            f"{settings.services.memory_search_backend!r} (AVA_MEMORY_SEARCH_BACKEND) — milvus not needed"
        )
    return None


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


def gate_reason_for_session(session: str) -> str | None:
    """The roster gate reason for ONE service session, or None when it will run.

    The start loop applies ``_gate_reason`` to the whole capability-union roster
    at once; a spawner that launches a single service OUTSIDE that loop (the
    pause lifecycle's restarter respawn) must ask the same question per session
    or it drifts from the roster — the hosted restarter relaunch after every
    rollout (2026-09-02, Task #2342: ``ava start`` skipped it correctly while the
    unpause finally respawned it). Capability membership is deliberately not
    part of the answer: the caller already knows it owns the session; only the
    config/capability gate can differ between an orchestration context and
    ``ava start``.
    """
    for spec in build_services():
        if spec.session == session:
            return _gate_reason(spec)
    return "not a roster service (no spec with this session in ops.spec)"


@dataclass(frozen=True)
class Spec:
    """A host's desired state — the roster it should run, plus the code revision
    and (as they converge here) the data plane it should run against.

    Slice 1 fills in the service roster; the cluster-pin accessor is a read-through
    to ``shared.cluster_pin`` (the pin's writer stays the rollout path). The
    data-plane desired state (per-cluster instance / ports / bind / redis-ACL
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

    def cluster_pin(self) -> str | None:
        """The SHA the cluster is pinned to (None = no rollout has pinned one yet).

        A read-through to ``shared.cluster_pin`` — the pin is written by the rollout
        path, not by Spec. The pin controller diffs a host's HEAD against this.
        """
        from shared.cluster_pin import get_cluster_target_sha

        return get_cluster_target_sha()


# Compatibility re-export: legacy importers (`from ops.spec import
# build_services`, scripts/prepare_plugin_fixture.py's generated
# `services.py` template, tests) take the canonical roster from this module.
# Placed at the BOTTOM deliberately: roster's build_services calls back into
# this module's helpers (_bind_runtime_command / _plugin_services /
# _assert_unique_sessions) lazily, so this edge must not run while this module
# is partially initialized (spec → roster at the top would be a load-time
# edge in the opposite direction of the call-time edge — keep both lazy).
from ops.roster import build_services as build_services  # noqa: E402 — deliberate bottom re-export
