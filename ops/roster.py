"""Canonical service roster and identity-probe declarations."""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from pathlib import Path

from ops.service_spec import _AGENT_RUNNER, _BOTH, _GATEWAY, ServiceSpec
from shared.cluster import frontend_service_cmd
from shared.config import settings
from shared.daemon_health import DaemonProbe, health_port, probe_daemon, probe_home
from shared.paths import otel_collector_binary, otel_collector_config

# The roster body moved verbatim and still resolves these policy helpers by
# name. Lazy delegation keeps their definitions in ops.spec without introducing
# a module-initialization cycle when either side is imported first.


def _bind_runtime_command(spec: ServiceSpec) -> ServiceSpec:
    from ops.spec import _bind_runtime_command as bind_runtime_command

    return bind_runtime_command(spec)


def _plugin_services() -> tuple[ServiceSpec, ...]:
    from ops.spec import _plugin_services as plugin_services

    return plugin_services()


def _assert_unique_sessions(core: tuple[ServiceSpec, ...], plugin: tuple[ServiceSpec, ...]) -> None:
    from ops.spec import _assert_unique_sessions as assert_unique_sessions

    assert_unique_sessions(core, plugin)


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
        # Gated by _gate_reason to the milvus memory-search backend — numpy
        # (default) / pgvector hosts do not launch the ~1GB milvus-lite server.
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
    # running agents: the inbound ops server, one agent host, the runner's
    # watchdog, and the shared headed browser.
    agent_runner_services = (
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
        # One agent host per runner owns every local agent's turn tasks.
        ServiceSpec(
            session="agent-host",
            cmd=".venv/bin/python -m services.agent_host.daemon",
            capabilities=_AGENT_RUNNER,
            profile="agent",  # the host runs the agent kernel in-process; the runner-derived marker crashes it at import
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
    return tuple(_bind_runtime_command(spec) for spec in core + plugin)
