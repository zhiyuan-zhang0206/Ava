"""Liveness + system status panel + dashboard endpoints.

- `/api/health` — liveness probe (public allowlist; pings DB)
- `/api/stats/dashboard` — sidebar stats card
- `/api/status` — services / shells / cluster panel

Cluster probe sub-fan-out lives here because /api/status is the
consumer; agent-runner probes go through a `status_probe` op
round-trip so it stays at constant wall-time regardless of N machines.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Annotated, Any, cast

import psycopg
from fastapi import APIRouter, HTTPException, Query, Request
from psycopg import Cursor
from psycopg_pool import ConnectionPool
from pydantic import ValidationError

from base.agents.observation.evidence import MACHINE_OFFLINE_AFTER_FAILURES
from base.api_contracts.status import MachineStatus
from base.cluster.machine import (
    is_agent_runner,
    is_gateway,
    is_observability_station,
    machine_name,
)
from base.daemon.endpoints import ServiceEndpoints
from base.db import Database
from base.deploy.git.cluster_drift import prod_source_head_sha
from base.host.resource_sample import ResourceSample
from base.packages.plugins import stats
from base.telemetry.observability import cluster_label
from gateway.cluster import _roster_rows, _stats_events, roster_probe
from gateway.cluster._health import get_health
from gateway.cluster.schemas import (
    ClusterPanel,
    PluginStat,
    PluginStatStatus,
    ServiceItem,
    ServicesStatus,
    StatsDashboard,
    StatsTokens,
    SystemStatus,
)
from gateway.cluster.snapshots import Snapshot, read_all
from gateway.schemas.stats import StatsWindowHours, window_delta
from ops import cluster_rpc as _cluster_rpc
from ops.cluster_pause import is_paused as cluster_is_paused
from ops.cluster_status import ClusterStatus, check_pidfile
from ops.cluster_status.schema_mismatch import status as schema_mismatch_status
from services.events_maintenance import resolution as _resolution

router = APIRouter()
ARCHIVE_TOTAL_ROWS = 4_813_148  # frozen archive rows at the #1823 drop (pg_dump-verified)
_log = logging.getLogger(__name__)
_STATUS_CACHE_TTL_S = 15.0


class StatusCache:
    """Single-flight TTL cache of the `/api/status` response, one per gateway process.

    Built by the app lifespan. Probe wall time follows the slowest machine and several
    frontend pollers request this roster, so one caller recomputes while the others wait
    on the lock and then read what it stored.
    """

    def __init__(
        self,
        *,
        ttl_s: float = _STATUS_CACHE_TTL_S,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_s = ttl_s
        self._monotonic = monotonic
        self._cached: tuple[float, SystemStatus] | None = None
        self._lock = threading.Lock()

    def get(self, compute: Callable[[], SystemStatus]) -> SystemStatus:
        """The cached response while it is fresh, else the result of `compute()`."""
        cached = self._cached
        if cached is not None and self._monotonic() - cached[0] < self._ttl_s:
            return cached[1]
        with self._lock:
            cached = self._cached
            if cached is not None and self._monotonic() - cached[0] < self._ttl_s:
                return cached[1]
            response = compute()
            self._cached = (self._monotonic(), response)
            return response


router.add_api_route("/api/health", get_health, methods=["GET"], response_model=None)


@router.get("/api/stats/dashboard")
def get_stats_dashboard(
    request: Request,
    hours: Annotated[StatsWindowHours, Query()] = StatsWindowHours.H24,
) -> StatsDashboard:
    """Pull all data for the sidebar-top stats card in one shot.

    Data sources:
    - `live_count`: agents_meta table — all non-terminated agents (running/idling)
    - `tokens` / `cost_usd` / average turn duration: the window's `llm_usage` and
      `turn_end` rows in `telemetry_events` (cost is each row's usage-time snapshot)
    - warning/error counts: per-class counts of `telemetry_events` rows, split into
      total / dismissed / net with the resolution daemon's class arithmetic over the
      SELECTED window (task #1935)
    - `total_events`: archived event row count — frozen historical constant
      (task #1281 parity run; PG events dropped; not a live gauge)

    `?hours=` selects the aggregation window (0 = last 5m; 1/6/24/72/168 =
    hours), whitelisted by `StatsWindowHours` (anything else 422s); the served horizon is
    `applied_window_hours`, which is the requested window. Zero-data scenario: tokens all 0,
    cost_usd 0.0, avg_turn_seconds None (frontend shows "—"). The window is computed on
    every request, in one connection, with an 8-second statement timeout.
    """
    try:
        return _compute_stats_dashboard(request.app.state.db_pool, hours)
    except psycopg.errors.QueryCanceled as exc:
        raise HTTPException(status_code=503, detail="stats read timed out; retry") from exc


def _compute_stats_dashboard(pool: ConnectionPool[Any], hours: StatsWindowHours) -> StatsDashboard:
    """Assemble the payload from one pooled connection."""
    cluster = cluster_label()
    now = datetime.now(UTC)
    window_start = now - window_delta(hours)
    with pool.connection() as conn:
        conn.execute("SET LOCAL statement_timeout = '8s'")
        totals = _stats_events.window_totals(conn, cluster=cluster, start=window_start, end=now)
        class_counts = _stats_events.window_class_counts(
            conn, cluster=cluster, start=window_start, end=now
        )
        live_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM agents_meta WHERE status != 'terminated'"
            ).fetchone()[  # type: ignore[index]
                0
            ]
        )
        # total_events is a historical constant — the frozen pre-cutover
        # archive's parity row count (task #1281), not a live gauge: the PG
        # events table was dropped with the #1823 cleanup; the dashboard's
        # "total events" card shows the archive's size. See ARCHIVE_TOTAL_ROWS.
        total_events = ARCHIVE_TOTAL_ROWS

        # Active class-wide dismissals, read like the daemon reads them.
        splits = _resolution.level_splits(
            class_counts,
            {dismissal.event_class for dismissal in _resolution.active_dismissals(conn)},
        )
    warning = splits.get("warning", _resolution.LevelSplit(0, 0, 0))
    error = splits.get("error", _resolution.LevelSplit(0, 0, 0))
    cache_hit_pct = round(totals.cache_read / totals.in_total * 100, 2) if totals.in_total else 0.0
    return StatsDashboard(
        live_count=live_count,
        window_hours=hours,
        applied_window_hours=int(hours),
        tokens=StatsTokens(
            input=totals.in_total,
            output=totals.out_total,
            cache_read=totals.cache_read,
            cache_hit_pct=cache_hit_pct,
        ),
        cost_usd=totals.cost_usd,
        avg_turn_seconds=totals.turn_seconds / totals.turn_count if totals.turn_count else None,
        warnings=warning.total,
        errors=error.total,
        warnings_dismissed=warning.dismissed,
        warnings_net=warning.net,
        errors_dismissed=error.dismissed,
        errors_net=error.net,
        total_events=total_events,
        plugin_stats=_plugin_stat_rows(pool),
        as_of=datetime.now(UTC),
    )


def _plugin_stat_rows(pool: ConnectionPool[Any]) -> list[PluginStat]:
    """The runtime values behind plugin-declared statistics cards, for the response.

    Not windowed: a plugin value is a point in time (`PluginStat`), and the
    console joins these rows against the `contributions.ui.stats`
    declarations by `(plugin, id)` — a declared card with no row here renders
    as an explicit empty state.
    """
    return [
        PluginStat(
            plugin=row.plugin,
            id=row.id,
            value=row.value,
            detail=row.detail,
            status=cast(PluginStatStatus, row.status),
            updated_at=row.updated_at,
            updated_by=row.updated_by,
        )
        for row in stats.read_all(pool)
    ]


def _get_services_status() -> ServicesStatus:
    """Gateway-only daemon health (pidfile + signal).

    Per-host daemons (agent-host, watchdog) are not here — they ride each
    machine's ClusterStatus probe and render in the roster. This block is the
    daemons that only run on the gateway."""
    items: list[ServiceItem] = []
    for name, label, pidfile in (
        ("labeler", "Labeler Daemon", ServiceEndpoints.from_settings().of("labeler").pidfile),
        (
            "memory_indexer",
            "Memory Indexer",
            ServiceEndpoints.from_settings().of("memory_indexer").pidfile,
        ),
    ):
        alive, pid = check_pidfile(str(pidfile))
        items.append(
            ServiceItem(
                name=name,
                label=label,
                online=alive,
                pid=pid,
                detail=None
                if alive
                else ("pidfile exists but process is dead" if pid else "pidfile not found"),
            )
        )
    return ServicesStatus(items=items)


# Per-machine status_probe timeout — `settings.gateway.status_probe_timeout_seconds`
# (default 8s). Raised from a 3.0s hardcode (task #1200): a slow-but-healthy WSL
# runner's status_snapshot measured 3.07-3.27s on 2026-08-12, and a budget
# shorter than the handler's own wall time flipped it offline while /healthz
# answered in ~15ms. The budget of a dial the gateway makes itself (a fresh read,
# or a machine the snapshot does not cover). The default read dials nothing: it
# renders the heartbeat liveness pass's snapshot (`gateway/cluster/snapshots.py`),
# which reads this same setting (services/heartbeat/liveness.py), so the pass's
# probes stay aligned with a fresh read's.


def _machine_status_from_cluster_status(
    identity_log: roster_probe.IdentityMismatchLog,
    status: ClusterStatus,
    name: str,
    role: list[str],
    gateway_url: str | None,
    up_since_at: datetime,
    description: str | None,
    stopped_at: datetime | None,
    *,
    is_staging: bool,
    observed_at: datetime | None,
) -> MachineStatus:
    """Render one machine's validated ClusterStatus as a roster row.

    Identity echo check: the ops server self-reports its machine_name in every
    status_probe response. If the responder is NOT the host we targeted, the
    gateway_url pointed at the wrong box (a loopback/misregistered row makes the
    gateway dial itself and answer under its own name). Refuse to render that as
    the target online — a loud identity-mismatch row instead. The log line
    itself is episode-deduped and degrades to INFO for a stopped row (a stale
    URL answering for someone else is that row's expected face — task #4143).
    """
    if status.machine_name != name:
        identity_log.log_mismatch(
            name, gateway_url, status.machine_name, stopped=stopped_at is not None
        )
        return _roster_rows.identity_mismatch_status(
            name,
            role,
            gateway_url,
            up_since_at,
            description,
            stopped_at,
            is_staging=is_staging,
        )
    identity_log.note_match(name)
    return MachineStatus(
        name=name,
        serve_gateway="gateway" in role,
        serve_agent_runner="agent-runner" in role,
        serve_observability_station="observability-station" in role,
        gateway_url=gateway_url or "",
        up_since_at=up_since_at,
        online=True,
        paused=status.paused,
        paused_reason=status.paused_reason,
        description=description,
        stopped_at=stopped_at,
        is_staging=is_staging,
        head_sha=status.head_sha,
        running_sha=status.running_sha,
        schema_mismatch=status.schema_mismatch,
        shell_count=status.shell_count,
        agent_host_online=status.agent_host_online,
        supervisor_online=status.supervisor_online,
        agent_count=status.agent_count,
        session_count=status.session_count,
        agent_groups=status.agent_groups,
        resource=status.resource,
        observed_at=observed_at,
    )


def _status_from_snapshot(
    identity_log: roster_probe.IdentityMismatchLog,
    snapshot: Snapshot,
    name: str,
    role: list[str],
    gateway_url: str | None,
    up_since_at: datetime,
    description: str | None,
    stopped_at: datetime | None,
    *,
    is_staging: bool,
) -> MachineStatus:
    """Render a roster row from the heartbeat pass's last probe of the machine.

    Two consecutive failed passes (or a failed pass with no earlier answer to
    show) read offline; one dropped probe keeps the last status on screen, and its
    age says so. A reachable answer that is not a ClusterStatus is the documented
    online + paused=None abnormal state.
    """
    row_args = (name, role, gateway_url, up_since_at, description, stopped_at)
    if not snapshot.reachable and (
        snapshot.status is None or snapshot.consecutive_failures >= MACHINE_OFFLINE_AFTER_FAILURES
    ):
        return _roster_rows.offline_status(*row_args, is_staging=is_staging)
    if snapshot.status is None:
        return _roster_rows.reachable_unknown_status(*row_args, is_staging=is_staging)
    try:
        status = ClusterStatus.model_validate(snapshot.status)
    except ValidationError:
        _log.warning(
            "machine_status_snapshot for %r holds a body that does not match ClusterStatus; "
            "reporting online+unknown",
            name,
            exc_info=True,
        )
        return _roster_rows.reachable_unknown_status(*row_args, is_staging=is_staging)
    return _machine_status_from_cluster_status(
        identity_log,
        status,
        *row_args,
        is_staging=is_staging,
        observed_at=snapshot.status_at or snapshot.observed_at,
    )


async def _probe_agent_runner(
    identity_log: roster_probe.IdentityMismatchLog,
    name: str,
    role: list[str],
    gateway_url: str | None,
    up_since_at: datetime,
    description: str | None,
    stopped_at: datetime | None,
    *,
    is_staging: bool = False,
) -> MachineStatus:
    """Probe an agent-runner now, by POSTing a `status_probe` op to its ops server.

    The machine is reached at its ava-ops server (services/agent_ops), which
    dispatches `status_probe` via `ops.cluster.cluster_status_op` in-process and
    returns the snapshot. Same path the heartbeat liveness pass uses. The local
    machine is no special case — its ops server is dialed at its registered
    localhost URL, keeping one uniform probe path.

    Online == the ops server responded within the timeout. Paused comes from the
    host's local `cluster_is_paused()` snapshot. Nothing is remembered: a down host
    costs this dial its full budget every time, which is why the default read does
    not come here.
    """
    row_args = (name, role, gateway_url, up_since_at, description, stopped_at)
    if gateway_url is None:
        # The roster row is the address authority for this fan-out. Do not let
        # cluster_rpc synchronously re-read Postgres outside the async timeout.
        return _roster_rows.offline_status(*row_args, is_staging=is_staging)
    try:
        result = await roster_probe.dispatch_status_probe(name, gateway_url)
    except _cluster_rpc.ClusterOpUnreachable:
        # Expected when a host is genuinely offline / mid-restart — quiet.
        return _roster_rows.offline_status(*row_args, is_staging=is_staging)
    except _cluster_rpc.ClusterOpFailed as exc:
        # Reached the ops server, but its status_probe op itself raised (DB error,
        # schema drift inside the op). That is NOT "offline" — surface it so the
        # real error is not invisible behind a misleading offline marker.
        _log.warning("status_probe op failed on reachable host %s: %s", name, exc.result)
        return _roster_rows.reachable_unknown_status(*row_args, is_staging=is_staging)
    # The ops server responded 200; validate its body as the status_probe result
    # contract (ClusterStatus) — same posture as cluster.py:get_cluster_status.
    # A body that does not validate (a version-skewed / wrong server) must NOT be
    # coerced into a determinate paused verdict: it lands in the documented
    # online=True + paused=None abnormal state instead of a false green.
    try:
        status = ClusterStatus.model_validate(result)
    except ValidationError:
        _log.warning(
            "status_probe on reachable host %r returned a body that does not match "
            "ClusterStatus; reporting online+unknown",
            name,
            exc_info=True,
        )
        return _roster_rows.reachable_unknown_status(*row_args, is_staging=is_staging)
    return _machine_status_from_cluster_status(
        identity_log, status, *row_args, is_staging=is_staging, observed_at=None
    )


def _local_resource_sample() -> ResourceSample | None:
    """One live resource reading for the gateway's own machine (no status_snapshot call)."""
    try:
        from base.host.resource_sample import resource_sample

        return resource_sample()
    except Exception:  # psutil may not be installed; degrade gracefully
        return None


def _local_machine_status_blocking(
    db: Database,
    name: str,
    url: str | None,
    role: list[str],
    up_since: datetime,
    description: str | None,
    stopped_at: datetime | None,
    *,
    is_staging: bool = False,
) -> MachineStatus:
    """Sync lightweight row for a local machine without the agent-runner
    capability (pure gateway) — via to_thread: the paused flag (file read),
    prod-source HEAD (git rev-parse subprocess), the frozen process commit and
    the psutil resource snapshot must not run on the event loop."""
    from base.native_process import loaded_commit as _process_sha

    paused = cluster_is_paused(db)
    return MachineStatus(
        name=name,
        serve_gateway="gateway" in role,
        serve_agent_runner="agent-runner" in role,
        serve_observability_station="observability-station" in role,
        gateway_url=url or "",
        up_since_at=up_since,
        online=True,
        paused=paused,
        # This row resolves paused from the posture row alone, so a true verdict
        # here has exactly one possible cause.
        paused_reason="business_pause" if paused else None,
        description=description,
        stopped_at=stopped_at,
        is_staging=is_staging,
        head_sha=prod_source_head_sha(),
        running_sha=_process_sha.get(),
        schema_mismatch=schema_mismatch_status(db),
        resource=_local_resource_sample(),
    )


async def gather_cluster_status(
    db: Database,
    rows: list[tuple[str, str | None, list[str], datetime, str | None, datetime | None, bool]],
    local_name: str,
    *,
    identity_log: roster_probe.IdentityMismatchLog,
    snapshots: Mapping[str, Snapshot] | None = None,
) -> list[MachineStatus]:
    """The roster of the given machines.

    With `snapshots` (the default read) each machine renders from the heartbeat
    liveness pass's last probe (`gateway/cluster/snapshots.py`) and nothing is dialed,
    so no down host can drag the read (task #3507); a machine with no fresh snapshot
    is dialed here, as a degraded fallback for a heartbeat service that is down. With
    `snapshots=None` (an explicit fresh read) every machine is probed in parallel via
    a status_probe op to its ops server (the local machine included — its ops server
    is dialed at its registered localhost URL); total wall ≈
    `settings.gateway.status_probe_timeout_seconds`.

    The one exception is a local machine without the agent-runner capability
    (a pure gateway in a split deployment): it runs no ops server, so its row
    is a lightweight local read — paused flag + prod-source HEAD — with no
    session/pidfile probes (shell/daemon liveness is agent-runner data a pure
    gateway does not have).

    Rows are the machines-table tuple (name, gateway_url, role, up_since_at,
    description, stopped_at, is_staging); description + stopped_at + is_staging
    are threaded through unmodified onto each MachineStatus. Public because
    `gateway/cluster/router.py:get_cluster_machines` reuses the same fan-out to back
    ava.agents.list_machines().

    The rows come back sorted by machine name.
    """
    machines: list[MachineStatus] = []
    probe_coros: list[Any] = []

    for name, url, role, up_since, description, stopped_at, is_staging in rows:
        if name == local_name and "agent-runner" not in role:
            machines.append(
                await asyncio.to_thread(
                    _local_machine_status_blocking,
                    db,
                    name,
                    url,
                    role,
                    up_since,
                    description,
                    stopped_at,
                    is_staging=is_staging,
                )
            )
        else:
            snapshot = None if snapshots is None else snapshots.get(name)
            if snapshot is not None and snapshot.fresh():
                machines.append(
                    _status_from_snapshot(
                        identity_log,
                        snapshot,
                        name,
                        role,
                        url,
                        up_since,
                        description,
                        stopped_at,
                        is_staging=is_staging,
                    )
                )
                continue
            probe_coros.append(
                _probe_agent_runner(
                    identity_log,
                    name,
                    role,
                    url,
                    up_since,
                    description,
                    stopped_at,
                    is_staging=is_staging,
                )
            )

    if probe_coros:
        machines.extend(await asyncio.gather(*probe_coros))

    return sorted(machines, key=lambda m: m.name)


def _get_cluster_status(
    db: Database, cur: Cursor, identity_log: roster_probe.IdentityMismatchLog
) -> ClusterPanel:
    """Assemble the cluster sub-section: SELECT the machines table (paused rows
    excluded — the cluster panel shows only active members; `ava cluster resume`
    brings a row back) and render each machine from the heartbeat liveness
    pass's last `status_probe` (`gateway/cluster/snapshots.py`), so the panel
    dials no runner (task #3507).

    Wrapped sync via asyncio.run because `/api/status` is a sync FastAPI
    handler (runs in threadpool); creating a fresh event loop here is safe.
    asyncio.run() fully closes the loop (and its selector fds) before returning,
    so concurrent panel polls each get a short-lived loop with no accumulation —
    the per-call loop construction is the only cost, negligible at panel cadence.
    Migrate the handler to `async def` only if that cadence rises enough to make
    loop setup measurable.
    """
    cur.execute(
        "SELECT name, gateway_url, role, up_since_at, description, stopped_at, is_staging "
        "FROM machines WHERE paused_at IS NULL ORDER BY name"
    )
    rows: list[tuple[str, str | None, list[str], datetime, str | None, datetime | None, bool]] = (
        cur.fetchall()
    )

    local_name = machine_name()
    snapshots = read_all(cur)
    machines = (
        asyncio.run(
            gather_cluster_status(
                db, rows, local_name, identity_log=identity_log, snapshots=snapshots
            )
        )
        if rows
        else []
    )

    return ClusterPanel(
        current_machine=local_name,
        current_serve_gateway=is_gateway(),
        current_serve_agent_runner=is_agent_runner(),
        current_serve_observability_station=is_observability_station(),
        current_paused=cluster_is_paused(db),
        machines=machines,
    )


@router.get("/api/status")
def get_system_status(request: Request) -> SystemStatus:
    """System status panel — pull services / shells / cluster in one shot.

    Probe wall time follows the slowest machine, while multiple frontend pollers
    request this roster. Fifteen-second staleness is acceptable for diagnostics;
    single-flight prevents expiry stampedes.
    Each block queries independently — a single failure does not affect
    the others (each has its own try/except that falls back to a
    degraded value).
    """
    status_cache: StatusCache = request.app.state.status_cache
    return status_cache.get(lambda: _compute_system_status(request))


def _compute_system_status(request: Request) -> SystemStatus:
    """One uncached build of the status panel."""
    # Services
    try:
        services = _get_services_status()
    except Exception:
        _log.exception("GET /api/status: services check failed")
        services = ServicesStatus(items=[])

    # Cluster
    try:
        with request.app.state.db_pool.connection() as conn, conn.cursor() as cur:
            cluster = _get_cluster_status(
                request.app.state.db, cur, request.app.state.identity_mismatch_log
            )
    except Exception:
        _log.exception("GET /api/status: cluster query failed")
        # Fallback: at least surface this host's name/role so the frontend
        # does not lose the whole section.
        try:
            cluster = ClusterPanel(
                current_machine=machine_name(),
                current_serve_gateway=is_gateway(),
                current_serve_agent_runner=is_agent_runner(),
                current_serve_observability_station=is_observability_station(),
                current_paused=cluster_is_paused(request.app.state.db),
                machines=[],
            )
        except Exception:
            _log.exception("GET /api/status: cluster fallback failed")
            cluster = ClusterPanel(
                current_machine="?",
                current_serve_gateway=False,
                current_serve_agent_runner=False,
                current_paused=False,
                machines=[],
            )

    return SystemStatus(services=services, cluster=cluster)
