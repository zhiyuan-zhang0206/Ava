"""Ops Spec — the single expression of a machine-role's desired state.

Given a host's capability set, this K8s-shaped source answers what should run.
``build_services()`` is the canonical roster and will grow to hold data-plane
and cluster-pin desired state as those converge here (future/infra/ops-module.md).

Every service declares its ``ServiceSpec.capabilities`` in one of three groups:
gateway-only, agent-runner-only, or both. ``services_for_capabilities(roles)``
selects services whose capabilities intersect the host's roles. A service also
declares ``requires_db`` so the watchdog can hold back exactly the database's
users during a DB-scoped round block (``ops.controllers.base.BlockScope``).

Plugins expose ``services() -> tuple[ServiceSpec, ...]`` from their services
module. ``_plugin_services()`` discovers code-present plugins and appends them
to ``build_services()``: plugin declares, ops discovers. Each plugin service's
own ``ServiceSpec.gate`` keeps cluster-level enablement out of ``_gate_reason``.
The fleet task daemon follows this path; see
``decisions/2026-07-19-plugin-registered-services.md``.

Layer: ``ops`` importing ``shared``, plus lazy function-local reaches into the
shared-tier browser identity probe and gate app-port source. Nothing reaches up
into cli/gateway, so start, watchdog, and ``ava status`` share this one roster.

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
definitions live only here.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from ops.runner_mode import runner_mode
from shared.cluster import frontend_service_cmd
from shared.config import settings
from shared.daemon_health import DaemonProbe, health_port, probe_daemon, probe_home
from shared.log import logger
from shared.machine import MachineRole, MachineRoles
from shared.observability import collector_allowed_for_home, gateway_observability_home
from shared.paths import otel_collector_binary, otel_collector_config
from shared.platform import IS_WINDOWS
from shared.platform_probes import (
    browser_incapability,
    browser_mcp_incapability,
    permissions_helper_incapability,
    unix_sockets_available,
)

# The capability groups a service can belong to. These are `MachineRole` values
# (`machine_role()` returns a frozenset of them); "capability" and "role" are the
# same axis in Ava's vocabulary (a unit carries a SET of capabilities).
_GATEWAY: frozenset[MachineRole] = frozenset({"gateway"})
_AGENT_RUNNER: frozenset[MachineRole] = frozenset({"agent-runner"})
_BOTH: frozenset[MachineRole] = frozenset({"gateway", "agent-runner"})


@dataclass(frozen=True)
class ServiceSpec:
    """Desired state for a single service.

    Attributes:
        session: bare service kebab (e.g. ``gateway``, ``frontend``); the real
            session name is composed by ``shared.cluster.session_name``.
        cmd: shell command run in the session (wrapped in ``cd <repo> && ...``).
        capabilities: which machine capabilities run this service. A host runs the
            service iff its role set intersects this — so a gateway-only host runs
            the ``{"gateway"}`` services, an agent-runner-only host runs the
            ``{"agent-runner"}`` services, and a single box (both roles) runs the
            union. This is the single readable place that says "which machine runs
            this", replacing the old exclusion-set encoding.
        requires_db: whether this service reads or writes the cluster's Postgres.
            Deliberately REQUIRED (no default): the watchdog holds back exactly the
            ``True`` services when a controller reports a DB-scoped round block
            (``BlockScope.DB_DEPENDENT`` — DB unreachable, or applied migrations
            disagreeing with this checkout), because reviving one of those would
            spawn a daemon that dies in its own ``assert_schema_current`` and
            crash-loops once a round. A default would silently classify the next
            service for the author, which is the coupling this field exists to
            remove — so a new service states the answer where it is declared, and a
            DB-free one keeps being revived through a DB outage. This is a fact about
            the SERVICE, not about its healthcheck, so the two watchdog specs answer
            it too even though nothing filters on them.
        pidfile: pidfile path (None = no pidfile, probe via other means).
        healthcheck_module: the ``services.healthchecks.<x>`` module whose
            ``main()`` the watchdog imports and runs every 60s to keep this
            service alive — the keepalive roster is DERIVED from this field. None
            = not watchdog-monitored (the watchdog daemons themselves).
        curl_url: HTTP probe URL (2xx/3xx = up); None = no curl probe.
        tcp_port: TCP-connect probe port for non-HTTP services (milvus gRPC).
        identity_probe: the probe that answers "is the thing on that port MINE",
            returning a ``DaemonProbe`` verdict. Set for every service whose
            endpoint can prove it — the ``/healthz`` daemons (name + home + pid,
            ``probe_daemon``), the gateway (home only, ``probe_home`` — uvicorn's
            reload fork makes the pid meaningless), and the browser (profile +
            listening socket, ``services.browser.probe``, because CDP carries no
            field we control). **None means the probe can assert liveness and
            nothing more**, and that is a property of the endpoint, not an
            oversight: the frontend serves Next.js, milvus speaks gRPC, and a
            watchdog's only signal is its own pidfile. Consumers show which of the
            two a row got, so an operator can tell an identity-verified ✓ from a
            2xx (`cli.commands._probe`). It exists as a field rather than being
            re-derived per consumer: the alternative let watchdog verify identity
            while operator surfaces trusted a bare 2xx from an occupant.
        gate: optional predicate returning a gate reason (a string = gated OUT of
            the start roster + why, None = will start). When set it OVERRIDES the
            built-in ``_gate_reason`` lookup, so a plugin service carries its own
            domain gate instead of adding a central branch; core services still
            flow through ``_gate_reason``.
        before_launch: optional preflight run immediately before creating the
            session, for a service-specific safe takeover.
        no_profile_marker: True = the launcher sets NO ``AVA_PROCESS_PROFILE``
            for this service's session, so the process boots profile-less (full
            Settings construction, no env-authority pop). Default False = the
            marker is derived from ``capabilities`` (gateway-only -> "gateway",
            agent-runner-only -> "runner"; both/neither -> no marker). Opt out
            for a gateway-side service whose code consumes agent-runner
            capability keys (the LLM provider keys, DEEPSEEK_API_KEY among
            them): the gateway profile's env-authority pass drops those from
            os.environ at boot, so ``settings.lm.*_api_key`` resolve to None and
            every model build fails (labeler, issue #1128 / task #1230). The
            labeler's watchdog respawn path already makes the same choice
            (services/healthchecks/labeler.py) — this field makes the initial
            ``ava start`` spawn agree with it.
    """

    session: str
    cmd: str
    capabilities: frozenset[MachineRole]
    requires_db: bool
    pidfile: Path | None = None
    healthcheck_module: str | None = None
    curl_url: str | None = None
    tcp_port: int | None = None
    gate: Callable[[], str | None] | None = None
    identity_probe: Callable[[], DaemonProbe] | None = None
    before_launch: Callable[[], None] | None = None
    no_profile_marker: bool = False


def profile_marker(spec: ServiceSpec) -> str | None:
    """The ``AVA_PROCESS_PROFILE`` value a launcher sets for ``spec``'s session.

    Derived from the spec's capabilities — gateway-only services run as
    ``gateway`` processes, agent-runner-only as ``runner``, and a service with
    both (or neither) gets no marker (neutral: the env-authority pop is
    skipped). ``spec.no_profile_marker`` overrides the derivation to None: the
    process boots profile-less, so the gateway profile's agent-runner-key pop
    never runs and ``settings.lm.*_api_key`` resolve from the unit's own .env.

    Returns:
        The marker value, or None when the launcher must set no marker.
    """
    if spec.no_profile_marker:
        return None
    if "gateway" in spec.capabilities and "agent-runner" not in spec.capabilities:
        return "gateway"
    if "agent-runner" in spec.capabilities and "gateway" not in spec.capabilities:
        return "runner"
    return None


def _hz(name: str) -> str:
    """Build the /healthz URL for a daemon using its settings-derived port."""
    return f"http://localhost:{health_port(name)}/healthz"


def daemon_identity(name: str, pidfile: Path) -> Callable[[], DaemonProbe]:
    """The ``identity_probe`` for a daemon that serves the standard Ava ``/healthz``.

    Binds ``probe_daemon`` to the three facts that identify one daemon: its
    ``name``, its ``/healthz`` URL (derived from this cluster's port block at
    call time, like every other probe target on the roster) and the pidfile this
    unit recorded for it. Public because plugin-registered services declare their
    own specs and must be able to state the same contract without restating the
    probe (``ava_builtins/plugins/*/services.py``).
    """
    return partial(probe_daemon, name, _hz(name), pidfile=pidfile)


def _browser_probe() -> DaemonProbe:
    """The headed browser's identity probe, imported at call time.

    The one place ``ops`` reaches into ``services``. It has to: CDP exposes
    nothing we control, so the browser is identified by the Chrome process
    running on this cluster's ``--user-data-dir`` — and both the profile path and
    the process-table identification already live in ``services/browser/``
    (``profile.py`` / ``orphan.py``), which imports only ``shared``. Restating
    either here would give the cluster's Chrome two definitions, which is the
    failure mode this whole batch is about. Lazy so importing the roster never
    pulls psutil in for a host that has no browser.
    """
    from services.browser.probe import probe_browser

    return probe_browser()


def build_services() -> tuple[ServiceSpec, ...]:
    """Return the canonical service roster with probe ports/URLs derived from settings.

    Called at use-time (not import time) so env-backed settings and monkeypatched
    ``health_port()`` values are picked up by tests and per-cluster port overrides.

    Authored in three capability groups so which machine runs a service is visible
    at a glance. The concatenation order is gateway-group, then agent-runner-group,
    then both-group; within each group the order is load-bearing where noted.
    (memory-indexer is not here: it is declared by the ava_memory plugin, which
    owns the pool it indexes — `plugins/ava_memory/services.py`.)
    """
    # The Next.js app port, read from the gate's own accessor: AVA_APP_PORT when set
    # (converge writes it from the cluster record), else the entry port + 1. Not
    # re-derived here — the gate PROXIES to this port, so a second derivation that
    # drifts is a gate forwarding to nothing.
    from services.gate.daemon import app_port
    from services.healthchecks.otel_collector import probe_collector, take_over_stale_collector

    _fe_port = app_port()
    _fe_url = f"http://localhost:{_fe_port}"
    # ── gateway-only services ───────────────────────────────────────────────
    # The gateway capability owns the data-plane-adjacent daemons: the HTTP
    # gateway, the frontend, and cluster-wide nudgers (heartbeat plus idle shell
    # reminders). They only INSERT inbound rows — the insert trigger wakes the
    # owner on any machine, so they belong to the single gateway, not each
    # runner. The gateway also owns the memory/vector stack and its watchdog.
    # The fleet task-maintenance nudger lives in the ava_fleet plugin — see
    # `_plugin_services()`.
    gateway_services = (
        ServiceSpec(
            session="gateway",
            cmd=".venv/bin/python -m gateway",
            capabilities=_GATEWAY,
            requires_db=True,  # gateway/app.py lifespan: assert_schema_current
            # gateway runs uvicorn(reload=True); the live process after reload is a
            # supervisor child, so the pidfile is unreliable (atexit unlinks on
            # reload). HTTP 200 on /api/agents is the real "ASGI up" signal.
            curl_url=settings.services.gateway_health_url,
            # Home only: the reload fork means a healthy gateway routinely answers
            # with a pid its own pidfile never recorded (`probe_home`).
            identity_probe=partial(probe_home, settings.services.gateway_health_url),
            healthcheck_module="services.healthchecks.gateway",
        ),
        ServiceSpec(
            session="im-bridge",
            cmd=".venv/bin/python -m services.im_bridge.daemon",
            capabilities=_GATEWAY,
            requires_db=True,  # R3 door ④: notice_bridge SELECT/UPDATEs agent_notices directly
            curl_url=_hz("im_bridge"),
            identity_probe=daemon_identity("im_bridge", settings.services.im_bridge_pidfile),
            healthcheck_module="services.healthchecks.im_bridge",
        ),
        ServiceSpec(
            session="labeler",
            cmd=".venv/bin/python -m services.labeler.daemon",
            capabilities=_GATEWAY,
            requires_db=True,  # assert_schema_current at boot, then polls the DB
            curl_url=_hz("labeler"),
            identity_probe=daemon_identity("labeler", settings.services.labeler_pidfile),
            healthcheck_module="services.healthchecks.labeler",
            # The labeler builds chat models (it generates labels), so it
            # consumes the agent-runner-capability LLM provider keys its own
            # .env declares. The gateway profile's env-authority pass would pop
            # them (DEEPSEEK_API_KEY among them) and every label generation
            # would fail with RuntimeError — issue #1128 (respawn path) /
            # task #1230 (initial start). No marker = full Settings, exactly
            # like the watchdog's respawn path (services/healthchecks/labeler.py).
            no_profile_marker=True,
        ),
        ServiceSpec(
            session="heartbeat",
            cmd=".venv/bin/python -m services.heartbeat.daemon",
            capabilities=_GATEWAY,
            requires_db=True,  # assert_schema_current at boot; INSERTs inbound rows
            curl_url=_hz("heartbeat"),
            identity_probe=daemon_identity("heartbeat", settings.services.heartbeat_pidfile),
            healthcheck_module="services.healthchecks.heartbeat",
        ),
        # delivery-watchdog: cluster-wide stale-pending-inbound tripwire. A
        # gateway daemon — it owns the data plane. Config-gated by
        # AVA_DELIVERY_WATCHDOG_ENABLED.
        ServiceSpec(
            session="delivery-watchdog",
            cmd=".venv/bin/python -m services.delivery_watchdog.daemon",
            capabilities=_GATEWAY,
            requires_db=True,  # assert_schema_current at boot; polls inbound_messages
            curl_url=_hz("delivery_watchdog"),
            identity_probe=daemon_identity(
                "delivery_watchdog", settings.services.delivery_watchdog_pidfile
            ),
            healthcheck_module="services.healthchecks.delivery_watchdog",
        ),
        # events-maintenance: gateway-owned maintenance daemon. ALWAYS runs (no
        # roster gate): its checkpoint reaper (Rule A fast loop + Rule B hourly)
        # and blob vacuum are unconditional — checkpoint_blobs grows ~150MB/h
        # without them (2026-08-12 regression). The PG events-archive slices
        # were removed with the task #1281/#1823 cleanup (table dropped; rows
        # live in the Loki archive stream).
        ServiceSpec(
            session="events-maintenance",
            cmd=".venv/bin/python -m services.events_maintenance.daemon",
            capabilities=_GATEWAY,
            requires_db=True,  # assert_schema_current at boot; checkpoint tables live in PG
            curl_url=_hz("events_maintenance"),
            identity_probe=daemon_identity(
                "events_maintenance", settings.services.events_maintenance_pidfile
            ),
            healthcheck_module="services.healthchecks.events_maintenance",
        ),
        # milvus before memory-indexer: memory-indexer cold-start connects to milvus.
        ServiceSpec(
            session="milvus",
            cmd=".venv/bin/python -m services.milvus.daemon",
            capabilities=_GATEWAY,
            # A separate vector store — supervises the milvus-lite binary and never
            # opens a Postgres connection, so a pg outage is not its business.
            requires_db=False,
            tcp_port=settings.services.milvus_port,
            healthcheck_module="services.healthchecks.milvus",
        ),
        # memory-search before memory-indexer too: the indexer's cold-start
        # connects to whichever backend the switch names, so the storage
        # services must come up first.
        ServiceSpec(
            session="memory-search",
            cmd=".venv/bin/python -m services.memory_search.daemon",
            capabilities=_GATEWAY,
            # The numpy backend's store: in-memory matrix + npz, no Postgres —
            # a pg outage is not its business (same as milvus).
            requires_db=False,
            tcp_port=settings.services.memory_search_port,
            healthcheck_module="services.healthchecks.memory_search",
        ),
        ServiceSpec(
            session="frontend",
            # Single source for the launch command: shared.cluster.frontend_service_cmd
            # — the watchdog respawn (services/healthchecks/frontend.py) builds the
            # SAME string, so the two launch paths cannot drift (they did once: the
            # respawn lost its `exec`, the session validator rejected the command,
            # and a dead frontend could never self-heal). On Windows the supervisor
            # runs the `&&` chain via `cmd /c` — cmd cannot do bash-style inline
            # `VAR=val cmd`; use `set "VAR=val" && ...` instead.
            # (NEXT_PUBLIC_* is build-time-inlined, so it must reach `npm run build`.)
            # Those inner quotes reach cmd intact only because the supervisor hands
            # the shell branch a verbatim command line: through a Popen argv list,
            # list2cmdline would escape them to \" , which cmd reads as a literal
            # backslash plus a quote toggle, setting a variable named `\`.
            cmd=frontend_service_cmd(_fe_port),
            capabilities=_GATEWAY,
            # Next.js reaches data only through the gateway HTTP API — no pg client in
            # ui/web/package.json. Reviving it during a DB outage brings the
            # operator UI back to report the outage rather than leaving it dark.
            requires_db=False,
            curl_url=_fe_url,
            healthcheck_module="services.healthchecks.frontend",
        ),
        ServiceSpec(
            session="pg-backup",
            cmd=".venv/bin/python -m services.backup_scheduler.daemon",
            capabilities=_GATEWAY,
            requires_db=True,  # dumps that very database
            pidfile=settings.services.pg_backup_pidfile,
            curl_url=_hz("pg_backup"),
            identity_probe=daemon_identity("pg_backup", settings.services.pg_backup_pidfile),
            healthcheck_module="services.healthchecks.pg_backup",
        ),
        ServiceSpec(
            session="pitr-uploader",
            cmd=".venv/bin/python -m services.pitr.uploader_daemon",
            capabilities=_GATEWAY,
            requires_db=False,
            pidfile=settings.services.pitr_uploader_pidfile,
            curl_url=_hz("pitr_uploader"),
            identity_probe=daemon_identity(
                "pitr_uploader", settings.services.pitr_uploader_pidfile
            ),
            healthcheck_module="services.healthchecks.pitr_uploader",
        ),
        ServiceSpec(
            session="pitr-base-candidate",
            cmd=".venv/bin/python -m services.pitr.base_scheduler_daemon",
            capabilities=_GATEWAY,
            requires_db=True,
            pidfile=settings.services.pitr_base_backup_pidfile,
            curl_url=_hz("pitr_base_backup"),
            identity_probe=daemon_identity(
                "pitr_base_backup", settings.services.pitr_base_backup_pidfile
            ),
            healthcheck_module="services.healthchecks.pitr_base_backup",
        ),
        # One watchdog PER CAPABILITY (not a role-union daemon): two co-located
        # units on one host would otherwise collide on a single host-singleton
        # watchdog session, leaving one capability's services unrevived. The
        # watchdog itself is not monitored (healthcheck_module=None).
        ServiceSpec(
            session="gateway-watchdog",
            cmd=".venv/bin/python -m services.watchdog.daemon --role gateway",
            capabilities=_GATEWAY,
            requires_db=True,  # its schema controller queries the DB every round
            pidfile=settings.services.gateway_watchdog_pidfile,
            healthcheck_module=None,
        ),
    )

    # ── agent-runner-only services ──────────────────────────────────────────
    # The agent-runner capability owns everything that only makes sense next to
    # running agents: the inbound ops server the gateway dials, the restarter that
    # respawns crashed agent panes (exactly one, on the runner — two would race on
    # the host's agents), the runner's own watchdog, and the shared headed browser.
    agent_runner_services = (
        ServiceSpec(
            session="restarter",
            cmd=".venv/bin/python -m services.restarter.daemon",
            capabilities=_AGENT_RUNNER,
            requires_db=True,  # assert_schema_current at boot; every controller scans the DB
            curl_url=_hz("restarter"),
            identity_probe=daemon_identity("restarter", settings.services.restarter_pidfile),
            healthcheck_module="services.healthchecks.restarter",
        ),
        # page-server: supervises page servers per agent_pages row (R3 door 3).
        # One per runner — it spawns/kills the detached page server processes
        # for rows whose host is this host.
        ServiceSpec(
            session="page-server",
            cmd=".venv/bin/python -m services.page_server.daemon",
            capabilities=_AGENT_RUNNER,
            requires_db=True,  # the agent_pages table is its truth source
            pidfile=settings.services.page_server_pidfile,
            curl_url=_hz("page_server"),
            identity_probe=daemon_identity("page_server", settings.services.page_server_pidfile),
            healthcheck_module="services.healthchecks.page_server",
        ),
        ServiceSpec(
            session="agent-runner-watchdog",
            cmd=".venv/bin/python -m services.watchdog.daemon --role agent-runner",
            capabilities=_AGENT_RUNNER,
            requires_db=True,  # its schema controller queries the DB every round
            pidfile=settings.services.agent_runner_watchdog_pidfile,
            healthcheck_module=None,
        ),
        # agent-host: the hosted agent-runner — one daemon running every local
        # agent's turns as asyncio tasks instead of one OS process per agent
        # (future/infra/agent-runner-as-server.md). Gated OFF unless
        # AVA_RUNNER_MODE is `hosted`, which is not the default, so this service
        # is absent from every cluster's roster until one opts in. One per
        # runner, like the restarter: two would race on the same agents' turns.
        ServiceSpec(
            session="agent-host",
            cmd=".venv/bin/python -m services.agent_host.daemon",
            capabilities=_AGENT_RUNNER,
            requires_db=True,  # assert_schema_current at boot; every turn reads/writes agents_meta
            pidfile=settings.services.agent_host_pidfile,
            curl_url=_hz("agent_host"),
            identity_probe=daemon_identity("agent_host", settings.services.agent_host_pidfile),
            healthcheck_module="services.healthchecks.agent_host",
        ),
        # ops: inbound server. Binds 0.0.0.0:<ops_port>, serves POST /ops; the
        # gateway dials it directly (HTTP-uniform, even on a co-located single box).
        ServiceSpec(
            session="ops",
            cmd=".venv/bin/python -m services.agent_ops.daemon",
            capabilities=_AGENT_RUNNER,
            requires_db=True,  # assert_schema_current at boot; serves DB-backed ops calls
            curl_url=_hz("ops"),
            identity_probe=daemon_identity("ops", settings.services.ops_pidfile),
            healthcheck_module="services.healthchecks.ops",
        ),
        # browser: config/capability-gated (AVA_BROWSER_ENABLED + display/Chrome/npx).
        # CDP exposes HTTP at /json/version, so probe via curl_url (not tcp_port).
        ServiceSpec(
            session="browser",
            cmd=".venv/bin/python -m services.browser.daemon",
            capabilities=_AGENT_RUNNER,
            # A headed Chrome under a supervisor: no DB at boot, none at runtime, and
            # its healthcheck probes CDP + session liveness only. It must therefore
            # keep being revived while the DB is down — that outage is the moment a
            # crashed browser most needs its recovery path.
            requires_db=False,
            curl_url=f"http://127.0.0.1:{settings.services.browser_cdp_port}/json/version",
            # CDP has no identity field, so a 2xx here says only "a debuggable
            # Chrome is up" — the profile-anchored check is what says it is ours.
            identity_probe=_browser_probe,
            healthcheck_module="services.healthchecks.browser",
        ),
        # browser-mcp: shared chrome-devtools-mcp upstream. Speaks MCP over a
        # Unix socket — no HTTP/TCP probe; its healthcheck dials the socket
        # directly, and that transport is why its gate is browser's PLUS AF_UNIX
        # (POSIX-only; see `_gate_reason`).
        ServiceSpec(
            session="browser-mcp",
            cmd=".venv/bin/python -m services.browser.mcp_daemon",
            capabilities=_AGENT_RUNNER,
            # Same story: a Unix-socket multiplexer in front of chrome-devtools-mcp.
            # Its whole data plane is that socket plus CDP.
            requires_db=False,
            healthcheck_module="services.healthchecks.browser_mcp",
        ),
        # computer-mcp: per-machine computer-use executor. Every desktop action
        # goes through the signed permissions helper; the daemon serializes
        # actions and writes computer_action audit events (facts, not
        # governance — per-agent permission division is a prompt-level peer
        # convention, user ruling 2026-08-10), so it needs the DB. Its gate
        # requires the platform to be capable (see _gate_reason).
        ServiceSpec(
            session="computer-mcp",
            cmd=".venv/bin/python -m services.computer.mcp_daemon",
            capabilities=_AGENT_RUNNER,
            requires_db=True,
            healthcheck_module="services.healthchecks.computer_mcp",
        ),
        # mcp-daemon: ONE shared MCP daemon for every agent on this machine
        # (replaces one ~12MB daemon child per agent). Sessions are isolated per
        # client connection, so sharing the process shares no state; its data
        # plane is the Unix socket plus per-server stdio children. The socket
        # transport is why its gate is AF_UNIX (POSIX-only; see `_gate_reason`)
        # — the same story as browser-mcp, keeping it out of a Windows roster.
        ServiceSpec(
            session="mcp-daemon",
            cmd=".venv/bin/python -m ava._mcps_daemon",
            capabilities=_AGENT_RUNNER,
            # Config is local files (mcp.json); no DB at boot or runtime.
            requires_db=False,
            healthcheck_module="services.healthchecks.mcp_daemon",
        ),
    )

    # ── both-capability services ────────────────────────────────────────────
    # Services a gateway-only host AND an agent-runner-only host each run.
    #
    # otel-collector: one per machine (task #1266). Every agent exports OTLP to
    # its LOCAL sidecar and writes the local JSONL trace mirror. A gateway
    # collector fans out to gateway-loopback Tempo/Loki/Prometheus; a pure
    # runner collector relays to the gateway collector's authenticated
    # private-address receiver. Backend ports never leave gateway loopback.
    # Not DB-dependent by design: trace/log queues buffer persistently while a
    # route is unavailable; the bounded metrics queue sheds rather than making
    # collector lifecycle depend on the data plane.
    both_services: tuple[ServiceSpec, ...] = (
        ServiceSpec(
            session="otel-collector",
            cmd=f"{otel_collector_binary()} --config {otel_collector_config()}",
            capabilities=_BOTH,
            requires_db=False,
            # The healthcheck POSTs a valid empty ExportTraceServiceRequest; bare TCP is insufficient.
            # The port follows AVA_TELEMETRY_OTLP_PORT (single source, task #1945).
            tcp_port=settings.observability.telemetry_otlp_port,
            identity_probe=probe_collector,
            before_launch=take_over_stale_collector,
            healthcheck_module="services.healthchecks.otel_collector",
        ),
    )

    core = gateway_services + agent_runner_services + both_services
    # Plugin-registered services (e.g. ava_fleet's task-maintenance) are appended
    # so this stays THE single roster: a plugin declares a ServiceSpec, ops
    # discovers it. Session-name collisions fail fast — the roster's keys must be
    # unique for the watchdog/status derivations keyed on `session`.
    plugin = _plugin_services()
    _assert_unique_sessions(core, plugin)
    return core + plugin


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
    can register too; it must import only light deps (ops.spec / shared), never its
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
    if session == "agent-host" and runner_mode() != "hosted":
        return "disabled (AVA_RUNNER_MODE is process)"
    if session == "restarter" and runner_mode() == "hosted":
        # All four restarter controllers reason about agent PROCESSES (pid
        # probes, lease rows, session relaunches); hosted rows have none of
        # those, so a process reaper would harvest every healthy agent.
        return "disabled (AVA_RUNNER_MODE is hosted — per-agent process supervision retired)"
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
