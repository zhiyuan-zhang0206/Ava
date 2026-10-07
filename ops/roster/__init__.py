"""Canonical service roster and identity-probe declarations.

This package door holds the roster. Its `service_spec` submodule defines the
`ServiceSpec` contract every roster entry carries, its `healthz` submodule builds
the entry of a standard `/healthz` daemon from four declarations, and `observe`
derives read-only service status from the roster.
"""

from __future__ import annotations

import shlex
from functools import partial

from base.cluster import frontend_service_cmd
from base.config import settings
from base.daemon.health import DaemonProbe, probe_home
from base.paths import ava_home, otel_collector_binary, otel_collector_config
from ops.roster.healthz import daemon_identity as daemon_identity  # public: plugin services
from ops.roster.healthz import healthz_daemon as healthz_daemon  # public: plugin services
from ops.roster.service_spec import _AGENT_RUNNER, _BOTH, _GATEWAY, ServiceSpec

# After uvicorn's connection drain the gateway runs its lifespan cleanup and the
# interpreter exits. That tail waits on the lifespan teardown's in-flight work
# (the schedule manager's stop, the flushers) plus the pool closes; this is its
# declared bound.
_GATEWAY_LIFESPAN_ALLOWANCE_S = 10.0

# The roster body moved verbatim and still resolves these policy helpers by
# name. Lazy delegation keeps their definitions in ops.spec without introducing
# a module-initialization cycle when either side is imported first.


def _plugin_services() -> tuple[ServiceSpec, ...]:
    from ops.spec import plugin_services

    return plugin_services()


def _assert_unique_sessions(core: tuple[ServiceSpec, ...], plugin: tuple[ServiceSpec, ...]) -> None:
    from ops.spec import _assert_unique_sessions as assert_unique_sessions

    assert_unique_sessions(core, plugin)


def _frontend_probe() -> DaemonProbe:
    """The frontend's identity probe, imported at call time.

    Next.js serves no /healthz it can sign, so the frontend is identified by
    process ownership: the app-port listener must belong to the frontend's
    captured ava-root unit (services/supervision/healthchecks/frontend.py). An old orphan
    that answers 200 outside that process lineage is not frontend health.
    """
    from services.supervision.healthchecks.frontend import probe_frontend

    return probe_frontend()


def _browser_probe() -> DaemonProbe:
    """The headed browser's identity probe, imported at call time.

    The one place ``ops`` reaches into ``services``. It has to: CDP exposes
    nothing we control, so the browser is identified by the Chrome process
    running on this cluster's ``--user-data-dir`` — and both the profile path and
    the process-table identification already live in ``services/desktop/browser/``
    (``profile.py`` / ``orphan.py``), which imports only ``base``. Restating
    either here would give the cluster's Chrome two definitions, which is the
    failure mode this whole batch is about. Lazy so importing the roster never
    pulls psutil in for a host that has no browser.
    """
    from services.desktop.browser.probe import probe_browser

    return probe_browser()


def build_services() -> tuple[ServiceSpec, ...]:
    """Return the canonical service roster with probe ports/URLs derived from settings.

    Called at use-time (not import time) so env-backed settings and monkeypatched
    endpoint values are picked up by tests and per-cluster port overrides.

    Authored in three capability groups so which machine runs a service is visible
    at a glance. The concatenation order is gateway-group, then agent-runner-group,
    then both-group; within each group the order is load-bearing where noted.
    (memory-indexer is not here: it is declared by the ava_memory plugin, which
    owns the pool it indexes — `plugins/ava_memory/services.py`.)
    """
    # Share the public entry/app port definition with the serving process.
    from services.agent_runner.pty_sessions import shutdown_budget as pty_sessions_budget
    from services.desktop.browser import shutdown_budget as browser_mcp_budget
    from services.entrypoints.gate.daemon import app_port, entry_port
    from services.supervision.healthchecks.gate import probe as probe_gate
    from services.supervision.healthchecks.otel_collector import probe_collector
    from services.supervision.healthchecks.protocol_probe import probe as probe_protocol_service

    _fe_port = app_port()
    _fe_url = f"http://localhost:{_fe_port}"
    # ── gateway-only services ───────────────────────────────────────────────
    # The gateway capability owns the data-plane-adjacent daemons: the HTTP
    # gateway, the frontend, and cluster-wide nudgers (heartbeat plus idle shell
    # reminders). They only INSERT inbound rows — the insert trigger wakes the
    # owner on any machine, so they belong to the single gateway, not each
    # runner. The gateway also owns the memory/vector stack.
    # The fleet task-maintenance nudger lives in the ava_fleet plugin — see
    # `_plugin_services()`.
    gateway_services = (
        ServiceSpec(
            session="gate",
            cmd=".venv/bin/python -m services.entrypoints.gate.daemon",
            capabilities=_GATEWAY,
            requires_db=False,
            curl_url=f"http://127.0.0.1:{entry_port()}/__ava/healthz",
            identity_probe=probe_gate,
            healthcheck_module="services.supervision.healthchecks.gate",
            # The gate's /__ava/healthz names its home (`probe_gate` checks it), so a
            # foreign gate on the entry port is recognisable before the launch.
            home_healthz=True,
        ),
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
            healthcheck_module="services.supervision.healthchecks.gateway",
            # SIGTERM lets uvicorn drain in-flight requests for up to the budget the
            # launch hands it (`gateway.cluster.server.serve_kwargs`), then run the lifespan
            # cleanup: root must wait at least that long.
            stop_ceiling_s=(
                settings.gateway.gateway_graceful_shutdown_timeout_seconds
                + _GATEWAY_LIFESPAN_ALLOWANCE_S
            ),
        ),
        healthz_daemon(
            "im-bridge",
            module="services.entrypoints.im_bridge.daemon",
            capabilities=_GATEWAY,
            requires_db=True,  # R3 door ④: notice_bridge SELECT/UPDATEs agent_notices directly
        ),
        healthz_daemon(
            "labeler",
            module="services.derived.labeler.daemon",
            capabilities=_GATEWAY,
            requires_db=True,  # assert_schema_current at boot, then polls the DB
            # The labeler builds chat models (it generates labels), so it
            # consumes the agent-runner-capability LLM provider keys its own
            # .env declares. The gateway profile's env-authority pass would pop
            # them (DEEPSEEK_API_KEY among them) and every label generation
            # would fail with RuntimeError — issue #1128 / task #1230. No marker
            # = full Settings.
            no_profile_marker=True,
        ),
        healthz_daemon(
            "heartbeat",
            module="services.wake.heartbeat.daemon",
            capabilities=_GATEWAY,
            requires_db=True,  # assert_schema_current at boot; INSERTs inbound rows
        ),
        # delivery-watchdog: cluster-wide stale-pending-inbound tripwire. A
        # gateway daemon — it owns the data plane. Config-gated by
        # AVA_DELIVERY_WATCHDOG_ENABLED.
        healthz_daemon(
            "delivery-watchdog",
            module="services.wake.delivery_watchdog.daemon",
            capabilities=_GATEWAY,
            requires_db=True,  # assert_schema_current at boot; polls inbound_messages
        ),
        # events-maintenance: gateway-owned maintenance daemon. ALWAYS runs (no
        # roster gate): its checkpoint reaper (Rule A fast loop + Rule B hourly)
        # and blob vacuum are unconditional — checkpoint_blobs grows ~150MB/h
        # without them (2026-08-12 regression). The PG events-archive slices
        # were removed with the task #1281/#1823 cleanup (table dropped; rows
        # live in the Loki archive stream).
        healthz_daemon(
            "events-maintenance",
            module="services.upkeep.events_maintenance.daemon",
            capabilities=_GATEWAY,
            requires_db=True,  # assert_schema_current at boot; checkpoint tables live in PG
        ),
        # ttl-reaper: enforces every wall-clock deadline (page / shell / browser
        # session / notice / impersonation TTLs) plus the hourly lifecycle-pointer
        # scans. A gateway daemon — it owns the data plane and dials the
        # runners' ops servers for shell kills.
        healthz_daemon(
            "ttl-reaper",
            module="services.upkeep.ttl_reaper.daemon",
            capabilities=_GATEWAY,
            requires_db=True,  # assert_schema_current at boot; every phase is a DB pass
        ),
        # schedule-manager: keeps one session per enabled `schedules` row alive
        # (launch under the crash backoff and breaker, reap the unwanted, consume
        # the API's sync requests). A gateway daemon — the sessions run from this
        # home's own checkout on the gateway host, and the service refuses to
        # start from any other checkout.
        healthz_daemon(
            "schedule-manager",
            module="services.wake.schedule_manager.daemon",
            capabilities=_GATEWAY,
            requires_db=True,  # assert_schema_current at boot; every decision is a DB read
        ),
        # memory-search before memory-indexer: the indexer's cold-start
        # connects to whichever backend the switch names, so the storage
        # services must come up first.
        ServiceSpec(
            session="memory-search",
            cmd=".venv/bin/python -m services.derived.memory_search.daemon",
            capabilities=_GATEWAY,
            # The numpy backend's store: in-memory matrix + npz, no Postgres —
            # a pg outage is not its business.
            requires_db=False,
            tcp_port=settings.services.memory_search_port,
            identity_probe=partial(probe_protocol_service, "memory-search"),
            healthcheck_module="services.supervision.healthchecks.memory_search",
        ),
        ServiceSpec(
            session="frontend",
            # Single source for the launch command: base.cluster.frontend_service_cmd
            # — the watchdog respawn (services/supervision/healthchecks/frontend.py) builds the
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
            identity_probe=_frontend_probe,
            healthcheck_module="services.supervision.healthchecks.frontend",
        ),
        healthz_daemon(
            "pg-backup",
            module="services.backup.scheduler.daemon",
            capabilities=_GATEWAY,
            requires_db=True,  # dumps that very database
        ),
    )

    # ── agent-runner-only services ──────────────────────────────────────────
    # The agent-runner capability owns everything that only makes sense next to
    # running agents: the inbound ops server, one agent host, the runner's
    # shared headed browser.
    agent_runner_services = (
        # page-server: supervises page servers per agent_pages row (R3 door 3).
        # One per runner — it spawns/kills the detached page server processes
        # for rows whose host is this host.
        healthz_daemon(
            "page-server",
            module="services.agent_runner.page_server.daemon",
            capabilities=_AGENT_RUNNER,
            requires_db=True,  # the agent_pages table is its truth source
        ),
        # One agent host per runner owns every local agent's turn tasks.
        healthz_daemon(
            "agent-host",
            module="services.agent_runner.agent_host.daemon",
            capabilities=_AGENT_RUNNER,
            profile="agent",  # the host runs the agent kernel in-process; the runner-derived marker crashes it at import
            requires_db=True,  # assert_schema_current at boot; every turn reads/writes agents_meta
        ),
        # ops: inbound server. Binds 0.0.0.0:<ops_port>, serves POST /ops; the
        # gateway dials it directly (HTTP-uniform, even on a co-located single box).
        healthz_daemon(
            "ops",
            module="services.agent_runner.agent_ops.daemon",
            capabilities=_AGENT_RUNNER,
            requires_db=True,  # assert_schema_current at boot; serves DB-backed ops calls
        ),
        # browser: config/capability-gated (AVA_BROWSER_ENABLED + display/Chrome/npx).
        # CDP exposes HTTP at /json/version, so probe via curl_url (not tcp_port).
        ServiceSpec(
            session="browser",
            cmd=".venv/bin/python -m services.desktop.browser.daemon",
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
            healthcheck_module="services.supervision.healthchecks.browser",
        ),
        # browser-mcp: shared chrome-devtools-mcp upstream. Speaks MCP over a
        # Unix socket — no HTTP/TCP probe; its healthcheck dials the socket
        # directly, and that transport is why its gate is browser's PLUS AF_UNIX
        # (POSIX-only; see `_gate_reason`).
        ServiceSpec(
            session="browser-mcp",
            cmd=".venv/bin/python -m services.desktop.browser.mcp_daemon",
            capabilities=_AGENT_RUNNER,
            # Same story: a Unix-socket multiplexer in front of chrome-devtools-mcp.
            # Its whole data plane is that socket plus CDP.
            requires_db=False,
            identity_probe=partial(probe_protocol_service, "browser-mcp"),
            healthcheck_module="services.supervision.healthchecks.browser_mcp",
            stop_ceiling_s=browser_mcp_budget.SHUTDOWN_CEILING_S,
        ),
        # computer-mcp: per-machine computer-use executor. Every desktop action
        # goes through the signed permissions helper; the daemon serializes
        # actions and writes computer_action audit events (facts, not
        # governance — per-agent permission division is a prompt-level peer
        # convention, user ruling 2026-08-10), so it needs the DB. Its gate
        # requires the platform to be capable (see _gate_reason).
        ServiceSpec(
            session="computer-mcp",
            cmd=".venv/bin/python -m services.desktop.computer.mcp_daemon",
            capabilities=_AGENT_RUNNER,
            requires_db=True,
            identity_probe=partial(probe_protocol_service, "computer-mcp"),
            healthcheck_module="services.supervision.healthchecks.computer_mcp",
        ),
        # mcp-daemon: ONE shared MCP daemon for every agent on this machine
        # (replaces one ~12MB daemon child per agent). Sessions are isolated per
        # client connection, so sharing the process shares no state; its data
        # plane is the Unix socket plus per-server stdio children. The socket
        # transport is why its gate is AF_UNIX (POSIX-only; see `_gate_reason`)
        # — the same story as browser-mcp, keeping it out of a Windows roster.
        ServiceSpec(
            session="mcp-daemon",
            cmd=".venv/bin/python -m ava.mcps._daemon",
            capabilities=_AGENT_RUNNER,
            # Config is local files (mcp.json); no DB at boot or runtime.
            requires_db=False,
            identity_probe=partial(probe_protocol_service, "mcp-daemon"),
            healthcheck_module="services.supervision.healthchecks.mcp_daemon",
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
        # pty-sessions: every agent shell on this machine, held by ONE ordinary
        # process (base/sessions/pty). A session outlives an agent, an agent host
        # or a gateway restarting; it ends with its shell or when this service
        # stops, which `ava stop` does only after closing the sessions and
        # writing their owners' notices. A Unix socket and a ledger file are its
        # whole data plane: no database at boot or runtime. The socket transport
        # (and pty itself) is why its gate is POSIX-only (see `_gate_reason`).
        ServiceSpec(
            session="pty-sessions",
            cmd=".venv/bin/python -m services.agent_runner.pty_sessions.daemon",
            capabilities=_BOTH,
            requires_db=False,
            identity_probe=partial(probe_protocol_service, "pty-sessions"),
            stop_ceiling_s=pty_sessions_budget.SHUTDOWN_CEILING_S,
        ),
        ServiceSpec(
            session="otel-collector",
            cmd=shlex.join(
                [str(otel_collector_binary()), "--config", str(otel_collector_config())]
            ),
            config_inputs=(otel_collector_config(),),
            capabilities=_BOTH,
            requires_db=False,
            # The healthcheck POSTs a valid empty ExportTraceServiceRequest; bare TCP is insufficient.
            # The port follows AVA_TELEMETRY_OTLP_PORT (single source, task #1945).
            tcp_port=settings.observability.telemetry_otlp_port,
            identity_probe=probe_collector,
            healthcheck_module="services.supervision.healthchecks.otel_collector",
        ),
    )

    from base.telemetry.lgtm_local import (
        BACKENDS,
        HEALTH_PATHS,
        backend_urls,
        service_argv,
        service_input_paths,
    )
    from services.supervision.healthchecks.lgtm import probe_backend

    urls = backend_urls()
    observability_services = tuple(
        ServiceSpec(
            session=name,
            cmd=shlex.join(service_argv(ava_home(), name)),
            config_inputs=service_input_paths(ava_home(), name),
            capabilities=frozenset({"gateway", "agent-runner", "observability-station"}),
            requires_db=False,
            curl_url=urls[name] + HEALTH_PATHS[name],
            identity_probe=partial(probe_backend, name),
            healthcheck_module="services.supervision.healthchecks.lgtm",
        )
        for name in BACKENDS
    )
    core = gateway_services + agent_runner_services + both_services + observability_services
    # Plugin-registered services (e.g. ava_fleet's task-maintenance) are appended
    # so this stays THE single roster: a plugin declares a ServiceSpec, ops
    # discovers it. Session-name collisions fail fast — the roster's keys must be
    # unique for the watchdog/status derivations keyed on `session`.
    plugin = _plugin_services()
    _assert_unique_sessions(core, plugin)
    return core + plugin
