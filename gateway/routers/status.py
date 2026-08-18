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
import time
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from psycopg import Cursor
from pydantic import ValidationError

from gateway import loki_events, prom_metrics
from gateway.routers import _roster_rows
from gateway.schemas import (
    ClusterPanel,
    MachineStatus,
    ServiceItem,
    ServicesStatus,
    StatsDashboard,
    StatsTokens,
    StatsWindowHours,
    SystemStatus,
)
from ops import cluster_rpc as _cluster_rpc
from ops.cluster import ClusterStatus, _check_pidfile, current_orchestration
from ops.cluster import is_paused as cluster_is_paused
from shared.cluster_drift import prod_source_head_sha
from shared.cluster_lock import DeployLease, settle_hosts
from shared.config import settings
from shared.last_update import LastUpdate
from shared.lm.pricing import cost_usd
from shared.machine import is_agent_runner, is_gateway, machine_name
from shared.paths import ava_home
from shared.resource_monitor import ResourcePoint

router = APIRouter()
_log = logging.getLogger(__name__)

# The OTLP-mapped llm_usage counters (shared/telemetry_otlp._record_metrics:
# int payload field -> Counter named ava_<event>_<field>, Prometheus appends
# `_total`) — the dashboard's token + cost block reads these from Prometheus.
_IN_METRIC = "ava_llm_usage_in_total"
_OUT_METRIC = "ava_llm_usage_out_total"
_CACHED_METRIC = "ava_llm_usage_cache_read_total"


@router.get("/api/health")
def get_health(request: Request) -> dict[str, str]:
    """Liveness probe over the private network (the gateway is unauthenticated). DB ping
    included — a gateway that can serve HTTP but cannot reach Postgres is not
    "healthy" enough for the watchdog to leave alone.

    The identity fields answer the three questions a 200 alone cannot: `name` is
    which *service* answered, `home` this gateway's `$AVA_HOME` (its cluster
    identity), `machine` its host. A 200 on this port only proves *something*
    listens there, so the watchdog verifies the identity too rather than the status
    code alone — see `services/healthchecks/gateway.py`. Each was additive to the
    `{"status": "ok"}` contract.

    `name` is a constant naming the service this route belongs to — the point being
    that an impostor answering here reports its own name, or none. No probe reads it
    yet; `shared.daemon_health._probe_home` gains the `name` arm only once every
    deployed gateway emits the field, and that ordering is load-bearing (#1038)."""
    with request.app.state.db_pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1")
    return {
        "status": "ok",
        "name": "gateway",
        "home": str(ava_home()),
        "machine": machine_name(),
    }


@router.get("/api/stats/dashboard")
def get_stats_dashboard(
    request: Request,
    hours: Annotated[StatsWindowHours, Query()] = StatsWindowHours.H24,
) -> StatsDashboard:
    """Pull all data for the sidebar-top stats card in one shot.

    Data sources:
    - `live_count`: agents_meta table — all non-terminated agents (allocated/starting/running/idling/restarting/hibernating)
    - `tokens` / `cost_usd`: Prometheus (task #1197 — the OTLP-mapped
      llm_usage counters via gateway/prom_metrics, windowed by `increase`)
    - rest: events append-only table (category=telemetry/log) — written by the unified emitter

    `?hours=` selects the aggregation window (`ts > now() - hours`), whitelisted
    by `StatsWindowHours` (1/6/24/72/168; anything else 422s). Zero-data
    scenario: tokens all 0, cost_usd 0.0, avg_turn_seconds None (frontend
    shows "—").
    """
    # Windowed per-model token sums from Prometheus — the OTLP-mapped llm_usage
    # counters (task #1197: the PG events read is replaced). Each model group
    # is priced via cost_usd (the single pricing source); cost is linear in
    # tokens, so summing per-group costs equals pricing the grand total. A
    # missing model label (events pre-dating model tracking) groups under ""
    # and contributes tokens at 0 cost; an unpriced model prices to None ->
    # 0 — unknown is excluded, not billed as free. An empty result set leaves
    # all totals at 0.
    in_by_model = prom_metrics.sum_by(_IN_METRIC, "model", window_hours=hours)
    out_by_model = prom_metrics.sum_by(_OUT_METRIC, "model", window_hours=hours)
    cached_by_model = prom_metrics.sum_by(_CACHED_METRIC, "model", window_hours=hours)
    in_total = out_total = cache_read = 0
    window_cost_usd = 0.0
    for model in set(in_by_model) | set(out_by_model) | set(cached_by_model):
        g_in = in_by_model.get(model, 0.0)
        g_out = out_by_model.get(model, 0.0)
        g_cached = cached_by_model.get(model, 0.0)
        in_total += round(g_in)
        out_total += round(g_out)
        cache_read += round(g_cached)
        window_cost_usd += cost_usd(model, round(g_in), round(g_out), round(g_cached)) or 0.0
    cache_hit_pct = round(cache_read / in_total * 100, 2) if in_total else 0.0

    with request.app.state.db_pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM agents_meta WHERE status != 'terminated'")
        live_count = int(cur.fetchone()[0])

        # The turn / W/E stats read Loki (task #1197 LGTM read side): the PG
        # `events` table is a frozen pre-cutover archive, so a live window
        # queried there flatlines to zero. The `ok` payload key = 'true'
        # filters out exception turns (raise / cancel / partial) so they do
        # not pollute the "per-turn average time" — `_llm.py:llm_node`'s
        # finally marks abnormal paths with ok=False; the sidebar number
        # represents the average wall time of "successfully completed turns".
        window_start = datetime.now(UTC) - timedelta(hours=hours)
        turn_end_sum = loki_events.attribute_aggregate(
            field="duration_seconds",
            agg="sum",
            event_names=["turn_end"],
            attribute_filters={"ok": "true"},
            from_=window_start,
        )
        turn_end_count = loki_events.count_events(
            event_names=["turn_end"],
            attribute_filters={"ok": "true"},
            from_=window_start,
        )
        avg_turn_seconds: float | None = turn_end_sum / turn_end_count if turn_end_count else None

        # events.level is lowercase (W9 switch; agent_events stored loguru's
        # uppercase 'WARNING'/'ERROR') — the level vocabulary is documented on
        # gateway/schemas/events.py and db/schema.sql.
        # critical folds into 'error' — the sidebar's warning/error gauge
        # must see critical-level events (daemon schema-drift exits etc.),
        # which used to be an observability blind spot (audit 2026-08-08).
        warn_err: dict[str, int] = {
            "warning": loki_events.count_events(
                level="warning", categories=["telemetry", "log"], from_=window_start
            ),
            "error": loki_events.count_events(
                level_min="error", categories=["telemetry", "log"], from_=window_start
            ),
        }

        # total_events is a coarse growth gauge, not an exact figure — sum the
        # planner's per-partition row estimates instead of a full-table COUNT(*) on
        # every 5s poll, which scaled O(rows) on the ever-growing append-only table.
        # events is partitioned, and a partitioned parent's own reltuples is
        # not maintained by autovacuum, so the count must come from the leaf
        # partitions. pg_partition_tree returns the whole tree (isleaf filters to the
        # data-holding leaves) and, for a non-partitioned table, a single leaf row
        # for the table itself — so this is correct whether or not events is
        # partitioned. GREATEST clamps the -1 "never analyzed" sentinel (Postgres
        # 14+); the value is approximate, refreshed by autovacuum/ANALYZE.
        cur.execute(
            "SELECT COALESCE(SUM(GREATEST(c.reltuples, 0)), 0)::bigint "
            "FROM pg_partition_tree('events'::regclass) t "
            "JOIN pg_class c ON c.oid = t.relid WHERE t.isleaf"
        )
        total_events = int(cur.fetchone()[0])

    return StatsDashboard(
        live_count=live_count,
        window_hours=hours,
        tokens=StatsTokens(
            input=int(in_total),
            output=int(out_total),
            cache_read=int(cache_read),
            cache_hit_pct=cache_hit_pct,
        ),
        cost_usd=window_cost_usd,
        avg_turn_seconds=avg_turn_seconds,
        warnings=warn_err.get("warning", 0),
        errors=warn_err.get("error", 0),
        total_events=total_events,
    )


def _get_services_status() -> ServicesStatus:
    """Gateway-only daemon health (pidfile + signal).

    Per-host daemons (restarter, watchdog) are not here — they ride each
    machine's ClusterStatus probe and render in the roster. This block is the
    daemons that only run on the gateway."""
    items: list[ServiceItem] = []
    for name, label, pidfile in (
        ("labeler", "Labeler Daemon", settings.services.labeler_pidfile),
        ("memory_indexer", "Memory Indexer", settings.services.memory_indexer_pidfile),
    ):
        alive, pid = _check_pidfile(str(pidfile))
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
# shorter than the handler's own wall time flipped it offline (probe timeout ->
# 2 consecutive failures -> machine_probe offline) while /healthz answered in
# ~15ms. A genuinely offline host still refuses fast (connect refused /
# blackhole), so the wider budget costs only the anti-jitter margin. The
# heartbeat liveness pass reads the same setting
# (services/heartbeat/liveness.py) so the two probes stay aligned by
# construction.

# Per-machine probe backoff. A machine that keeps failing its status_probe (a
# down host — e.g. a flaky WSL peer) would otherwise be dialed on every panel
# poll (~5s), one wasted round-trip + one log line each. Instead a recently-failed
# host is re-probed on an exponential schedule: min(5 * 2**failures, 300) seconds.
# Only reachability failures (ClusterOpUnreachable) widen the window; any
# reachable answer (a probe success, or an op-level ClusterOpFailed — the host
# responded) clears it back to the normal cadence. State is process-local monotonic
# time; a gateway restart drops it, which just re-probes everyone once and rebuilds
# the schedule. Concurrent panel polls (sync handler, threadpool) may race on this
# dict, but the ops are GIL-atomic and the worst case is one redundant probe or an
# off-by-one failure count — acceptable for a diagnostic throttle.
_PROBE_BACKOFF_BASE_S = 5.0
_PROBE_BACKOFF_CAP_S = 300.0
_probe_failures: dict[str, tuple[int, float]] = {}  # name -> (consecutive_failures, last_attempt)


def _probe_in_backoff(name: str) -> bool:
    """True when `name` failed recently enough that its next probe is still
    deferred. A name with no failure record is never deferred (normal cadence)."""
    state = _probe_failures.get(name)
    if state is None:
        return False
    failures, last_attempt = state
    backoff = min(_PROBE_BACKOFF_BASE_S * (2**failures), _PROBE_BACKOFF_CAP_S)
    return (time.monotonic() - last_attempt) < backoff


def _note_probe_unreachable(name: str) -> None:
    """Record an unreachable probe: bump the consecutive-failure count and stamp
    the attempt, widening the next backoff window."""
    failures = _probe_failures.get(name, (0, 0.0))[0]
    _probe_failures[name] = (failures + 1, time.monotonic())


def _note_probe_reachable(name: str) -> None:
    """Clear any backoff for `name` — it answered, so resume the normal cadence."""
    _probe_failures.pop(name, None)


async def _probe_agent_runner(
    name: str,
    role: list[str],
    gateway_url: str | None,
    up_since_at: datetime,
    description: str | None,
    stopped_at: datetime | None,
    *,
    is_staging: bool = False,
) -> MachineStatus:
    """Probe an agent-runner by POSTing a `status_probe` op to its ops server.

    The machine is reached at its ava-ops server (services/agent_ops), which
    dispatches `status_probe` via `gateway.ops_cluster.cluster_status_op`
    in-process and returns the snapshot. Same path the CLI `ava cluster status`
    uses. The local machine is no special case — its ops server is dialed at
    its registered localhost URL, keeping one uniform probe path.

    Online == the ops server responded within the timeout. Paused comes from
    the host's local `cluster_is_paused()` snapshot.
    """
    if _probe_in_backoff(name):
        # Recently-failed host still inside its backoff window: skip the dial and
        # report the same offline row a live probe would. One success re-probe
        # (once the window elapses) clears the backoff.
        return _roster_rows.offline_status(
            name, role, gateway_url, up_since_at, description, stopped_at, is_staging=is_staging
        )
    try:
        result = await _cluster_rpc.dispatch_to_machine(
            target_machine=name,
            kind="status_probe",
            payload={},
            timeout_s=settings.gateway.status_probe_timeout_seconds,
            # No transport retry: the roster carries its own per-machine
            # exponential backoff (_PROBE_BACKOFF_BASE_S), and an offline host
            # is steady-state — re-dialing it 4x just stalls the roster.
            retries=0,
        )
    except _cluster_rpc.ClusterOpUnreachable:
        # Expected when a host is genuinely offline / mid-restart — quiet. Widen
        # this host's backoff so a persistently-down peer stops being dialed every poll.
        _note_probe_unreachable(name)
        return _roster_rows.offline_status(
            name, role, gateway_url, up_since_at, description, stopped_at, is_staging=is_staging
        )
    except _cluster_rpc.ClusterOpFailed as exc:
        # Reached the ops server, but its status_probe op itself raised (DB error,
        # schema drift inside the op). That is NOT "offline" — surface it so the
        # real error is not invisible behind a misleading offline marker. The host
        # is reachable, so clear backoff (this is not the down-host case).
        _log.warning("status_probe op failed on reachable host %s: %s", name, exc.result)
        _note_probe_reachable(name)
        return _roster_rows.offline_status(
            name, role, gateway_url, up_since_at, description, stopped_at, is_staging=is_staging
        )
    _note_probe_reachable(name)
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
        return _roster_rows.malformed_probe_status(
            name,
            role,
            gateway_url,
            up_since_at,
            description,
            stopped_at,
            is_staging=is_staging,
        )
    # Identity echo check: the ops server self-reports its machine_name in every
    # status_probe response. If the responder is NOT the host we targeted, the
    # gateway_url pointed at the wrong box (a loopback/misregistered row makes the
    # gateway dial itself and answer under its own name). Refuse to render that as
    # the target online — a loud identity-mismatch row instead.
    if status.machine_name != name:
        _log.error(
            "identity mismatch: probing machine %r at %s, but the ops server self-reported %r",
            name,
            gateway_url,
            status.machine_name,
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
    return MachineStatus(
        name=name,
        serve_gateway="gateway" in role,
        serve_agent_runner="agent-runner" in role,
        gateway_url=gateway_url or "",
        up_since_at=up_since_at,
        online=True,
        paused=status.paused,
        description=description,
        stopped_at=stopped_at,
        is_staging=is_staging,
        head_sha=status.head_sha,
        running_sha=status.running_sha,
        shell_count=status.shell_count,
        restarter_online=status.restarter_online,
        watchdog_online=status.watchdog_online,
        agent_count=status.agent_count,
        session_count=status.session_count,
        agent_groups=status.agent_groups,
        resource_history=status.resource_history,
    )


def _pin_verdict(head_sha: str | None, cluster_target_sha: str | None) -> bool | None:
    """Whether a node is on the cluster pin. None when there is no pin yet or the
    node's head_sha is unknown (the comparison is meaningless); else head == pin."""
    if cluster_target_sha is None or head_sha is None:
        return None
    return head_sha == cluster_target_sha


def _read_cluster_pin() -> str | None:
    """The cluster pin (`cluster_target_sha`), or None if unset / DB unreachable.

    The pin is a diagnostic overlay on the roster, not load-bearing, so it must
    not take the whole status panel down. A transient connectivity blip
    (`OperationalError`, e.g. mid-rollout) degrades silently to "no pin shown". Any
    other failure — a missing pin row, a schema/permission error — is a real bug,
    not "no pin", so it is logged loudly (not swallowed silently) before degrading."""
    import psycopg

    from shared.cluster_pin import get_cluster_target_sha

    try:
        return get_cluster_target_sha()
    except psycopg.OperationalError:
        return None
    except Exception:
        _log.exception("reading cluster pin failed (roster pin column will be blank)")
        return None


def _read_known_good() -> str | None:
    """The cluster's rollback anchor (`last_known_good_sha`), or None when unset /
    unreadable. Same shape and same degradation as `_read_cluster_pin`.

    Surfaced because it was recorded and never shown anywhere. Without it a
    rollback presents as the pin simply *changing* to an older commit, with nothing
    saying that commit is the anchor the cluster deliberately fell back to — half of
    what made the 2026-07-30 recovery read as an anomaly rather than as the designed
    behaviour it was.
    """
    import psycopg

    from shared.cluster_pin import get_last_known_good_sha

    try:
        return get_last_known_good_sha()
    except psycopg.OperationalError:
        return None
    except Exception:
        _log.exception("reading last_known_good_sha failed (the anchor will not be shown)")
        return None


def _read_deploy_lease() -> DeployLease | None:
    """The live deploy lease (`shared.cluster_lock.read_update_lease`), or None when
    the cluster is free / the row cannot be read.

    Read once per roster assembly and stamped onto every row, the same shape as
    `_read_cluster_pin` and degrading the same way: a hold is a diagnostic overlay,
    so a transient `OperationalError` (a rollout mid-restart is exactly when this is
    asked) leaves the `hold` column blank rather than taking the roster down, while
    any other failure is a real bug and is logged loudly first.

    Deliberately NOT `ops.deploy_window.deploy_in_flight()`. That call probes every
    machine and, on a converged cluster, *releases* the settle hold — neither belongs
    on a read-only roster GET, and its per-host probes would also answer under the
    permissive polarity (an unreachable host reads "not deploying") while the column
    beside `pin` needs the hold's recorded set. Rendering the lease row keeps the
    roster a display of state rather than a second derivation of it.
    """
    import psycopg

    from shared.cluster_lock import read_update_lease

    try:
        return read_update_lease()
    except psycopg.OperationalError:
        return None
    except Exception:
        _log.exception("reading the deploy lease failed (roster hold column will be blank)")
        return None


def _read_last_update() -> LastUpdate | None:
    """The cluster's last update outcome, or None when unrecorded / unreadable.

    Read once per roster assembly and stamped onto every row, the same shape as
    `_read_cluster_pin` and `_read_deploy_lease` and degrading the same way: a
    transient `OperationalError` (a rollout mid-restart is exactly when the status
    surfaces are asked) leaves the banner off rather than taking the roster down,
    while any other failure is a real bug and is logged loudly first.

    Degrading to None means "we cannot say", and the surfaces show nothing rather
    than a green all-clear — the failure mode this record exists to close is a
    surface that stays quiet about a failed update, so it must not be reintroduced
    by the reader.
    """
    import psycopg

    from shared.last_update import read_last_update

    try:
        return read_last_update()
    except psycopg.OperationalError:
        return None
    except Exception:
        _log.exception("reading the last update record failed (the update banner will be blank)")
        return None


def _local_resource_history() -> list[ResourcePoint]:
    """Resource history for the gateway's own machine (no status_snapshot call)."""
    try:
        from shared.resource_monitor import resource_snapshot

        return resource_snapshot()
    except Exception:  # fail-fast-ok: psutil may not be installed; degrade gracefully
        return []


def _local_machine_status_blocking(
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
    from shared import process_sha as _process_sha

    return MachineStatus(
        name=name,
        serve_gateway="gateway" in role,
        serve_agent_runner="agent-runner" in role,
        gateway_url=url or "",
        up_since_at=up_since,
        online=True,
        paused=cluster_is_paused(),
        description=description,
        stopped_at=stopped_at,
        is_staging=is_staging,
        head_sha=prod_source_head_sha(),
        running_sha=_process_sha.get(),
        resource_history=_local_resource_history(),
    )


async def gather_cluster_status(
    rows: list[tuple[str, str | None, list[str], datetime, str | None, datetime | None, bool]],
    local_name: str,
    *,
    cluster_target_sha: str | None = None,
    deploy_lease: DeployLease | None = None,
    last_update: LastUpdate | None = None,
    last_known_good_sha: str | None = None,
) -> list[MachineStatus]:
    """Async fan-out: every machine probed in parallel via a status_probe op
    to its ops server (the local machine included — its ops server is dialed
    at its registered localhost URL). Total wall ≈
    `settings.gateway.status_probe_timeout_seconds`.

    The one exception is a local machine without the agent-runner capability
    (a pure gateway in a split deployment): it runs no ops server, so its row
    is a lightweight local read — paused flag + prod-source HEAD — with no
    session/pidfile probes (shell/daemon liveness is agent-runner data a pure
    gateway does not have).

    Rows are the machines-table tuple (name, gateway_url, role, up_since_at,
    description, stopped_at, is_staging); description + stopped_at + is_staging
    are threaded through unmodified onto each MachineStatus. `cluster_target_sha` (the cluster pin,
    read once by the caller) stamps each row's `on_pin` verdict. Public because
    `routers/cluster.py:get_cluster_machines` reuses the same fan-out to back
    ava.agents.list_machines().

    `deploy_lease` (the live lease, likewise read once by the caller) is the other
    cluster-global fact stamped per row: its `describe()` onto `deploy_hold`, and
    membership in the settle note's host list onto each row's `settle_waited_on`.
    Both are transcribed from the lease row — the fan-out's own probes do not inform
    them, and nothing here re-derives whether a deploy is in flight.

    `last_update` is the third such fact, and the one that turns the roster from a
    set of symptoms into a statement: a head/pin mismatch is shared by a node that
    missed a rollout, a checkout moved without a restart, and a rollout that failed
    and rolled back, so the roster carries the recorded outcome rather than leaving
    every reader to guess which (#1012)."""
    machines: list[MachineStatus] = []
    probe_coros: list[Any] = []

    for name, url, role, up_since, description, stopped_at, is_staging in rows:
        if name == local_name and "agent-runner" not in role:
            machines.append(
                await asyncio.to_thread(
                    _local_machine_status_blocking,
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
            probe_coros.append(
                _probe_agent_runner(
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

    hold_detail = deploy_lease.describe() if deploy_lease is not None else None
    # The hold's OWN population, read back from its note — never the machine table.
    # A row absent from it is "not named by this hold", which is not the same as
    # "converged" (a host that never acked was never covered).
    waited_on = frozenset(settle_hosts(deploy_lease.note) if deploy_lease is not None else [])
    machines = [
        m.model_copy(
            update={
                "on_pin": _pin_verdict(m.head_sha, cluster_target_sha),
                "deploy_hold": hold_detail,
                "settle_waited_on": m.name in waited_on,
                "last_update": last_update,
                "cluster_last_known_good_sha": last_known_good_sha,
            }
        )
        for m in machines
    ]
    machines.sort(key=lambda m: m.name)
    return machines


def _get_cluster_status(cur: Cursor) -> ClusterPanel:
    """Assemble the cluster sub-section: each machine's `status_probe` op
    round-trip (the local machine's ops server is dialed at localhost).

    SELECT machines table (paused rows excluded — the cluster panel shows
    only active members; `ava cluster resume` brings a row back) + dispatch
    parallel probes (agent-runner via a `status_probe` op; the host responds
    with its local paused state). Total wall ≈
    `settings.gateway.status_probe_timeout_seconds` regardless of N machines.

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

    pin = _read_cluster_pin()
    lease = _read_deploy_lease()
    last_update = _read_last_update()
    known_good = _read_known_good()
    local_name = machine_name()
    machines = (
        asyncio.run(
            gather_cluster_status(
                rows,
                local_name,
                cluster_target_sha=pin,
                deploy_lease=lease,
                last_update=last_update,
                last_known_good_sha=known_good,
            )
        )
        if rows
        else []
    )

    return ClusterPanel(
        current_machine=local_name,
        current_serve_gateway=is_gateway(),
        current_serve_agent_runner=is_agent_runner(),
        current_paused=cluster_is_paused(),
        current_orchestration=current_orchestration(),
        machines=machines,
        cluster_target_sha=pin,
        cluster_last_known_good_sha=known_good,
        last_update=last_update,
    )


@router.get("/api/status")
def get_system_status(request: Request) -> SystemStatus:
    """System status panel — pull services / shells / cluster in one shot.

    Each block queries independently — a single failure does not affect
    the others (each has its own try/except that falls back to a
    degraded value).
    """

    # Services
    try:
        services = _get_services_status()
    except Exception:
        _log.exception("GET /api/status: services check failed")
        services = ServicesStatus(items=[])

    # Cluster
    try:
        with request.app.state.db_pool.connection() as conn, conn.cursor() as cur:
            cluster = _get_cluster_status(cur)
    except Exception:
        _log.exception("GET /api/status: cluster query failed")
        # Fallback: at least surface this host's name/role so the frontend
        # does not lose the whole section.
        try:
            cluster = ClusterPanel(
                current_machine=machine_name(),
                current_serve_gateway=is_gateway(),
                current_serve_agent_runner=is_agent_runner(),
                current_paused=cluster_is_paused(),
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

    return SystemStatus(
        services=services,
        cluster=cluster,
    )
