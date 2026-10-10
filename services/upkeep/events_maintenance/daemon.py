"""Events-maintenance daemon — gateway-owned unified event-stream + checkpoint maintenance.

Always-on gateway daemon (cluster-wide; the gateway owns the data plane). Resident
loops under one `TaskGroup` (one that raises ends the process):

- Hourly loop (`AVA_EVENTS_MAINTENANCE_INTERVAL_SECONDS`, default 1h): recover missed
  observations (`services.upkeep.events_maintenance.observed_metrics`), replay the JSONL mirror
  into `telemetry_events` (`services.upkeep.events_maintenance.telemetry_replay`), recompute the
  day-grain rollup tables of the last closed days (`services.upkeep.events_maintenance.rollup`),
  run the incremental blob VACUUM (`services.upkeep.events_maintenance.blob_vacuum`), and the hourly
  checkpoint size/row-count telemetry sample.
- Resolution loop (`AVA_EVENTS_RESOLUTION_INTERVAL_SECONDS`, default 5m):
  refresh immutable-event class-resolution state and gauges from `telemetry_events`.
- Registry-gauge loop (every 60s): sample `max(agents.id)` and emit the
  `agent_registry` event the growth dashboard reads
  (`services.upkeep.events_maintenance.registry_gauge`).
- Alert-reconciliation loop (every 5m, only on a unit holding
  `GRAFANA_ADMIN_PASSWORD`): resolve stored Grafana alert rows the embedded
  Alertmanager no longer reports firing, for webhooks whose RESOLVE was lost
  (`services.upkeep.events_maintenance.alert_reconciler`).

The checkpoint trim opt-in was retired on 2026-09-30 under the never-delete
ruling. Its implementation remains in `checkpoint_reaper.py` but is not scheduled.

Each loop reports independent progress, success, and errors in `/healthz`.
Only completed bounded work or sleeps beat; exceeding a hard deadline fails
healthz until the watchdog replaces the process and its orphaned worker thread.
The service's TaskGroup retains every pass proxy, including expired passes; a
late worker failure is reported with its original traceback without restoring
health. Stop cancels those proxies and the existing hard exit skips thread joins.
The same trackers are projected as unified envelope components, so the legacy
per-loop snapshots and the component degradation reasons describe one state.

The checkpoint size sample and blob vacuum remain active independently of the
events pipeline; neither deletes live checkpoint history.

The rollup only covers whole days up to yesterday (UTC); today is served live by
the readers. The upsert is a full-day overwrite recompute keyed on the PK, so
re-running is idempotent — a restart or a fast interval never double-counts.
An indexed slice with zero aggregate rows is warned and skipped rather than
treated as an empty day, leaving that day's existing ledger rows intact.

Usage:
    .venv/bin/python -m services.upkeep.events_maintenance.daemon

Kept alive by the root supervisor's health monitor through the roster's
`/healthz` identity probe, so the schema-drift exit in
`_dispatch_loop` is revived on the next round instead of staying dead.

Health reports progress independently for the rollup and resolution loops.
The endpoint takes the worst state: only a completed bounded unit refreshes
a loop's success timestamp, and a worker running beyond its hard
deadline makes `/healthz` return 503 for watchdog recovery.
"""

import asyncio
import logging
import os
import signal
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import psycopg
from psycopg_pool import ConnectionPool

from base.cluster.machine import validate_machine_name
from base.config import settings
from base.daemon.endpoints import ServiceEndpoint, ServiceEndpoints
from base.daemon.health import (
    LivenessGroup,
    LoopProgress,
    start_health_server,
    stop_health_server,
)
from base.daemon.health_schema import DEGRADED, OK, component
from base.daemon.shutdown import cancel_and_drain, install_graceful_shutdown
from base.daemon.shutdown import hard_exit as _hard_exit
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.deploy.maintenance import admission
from base.events.live.bus import EventBus
from base.log import init_gateway_process
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from base.telemetry.emitter import build_pipeline
from services.pidfile import acquire_pidfile, pidfile_holds_daemon, remove_pidfile
from services.upkeep.events_maintenance import alert_reconciler, registry_gauge
from services.upkeep.events_maintenance.blob_vacuum import (
    emit_checkpoint_table_sizes,
    run_blob_vacuum,
)
from services.upkeep.events_maintenance.config import EventsMaintenanceConfig
from services.upkeep.events_maintenance.observed_metrics import recover_observations
from services.upkeep.events_maintenance.resolution import AutoDismissCadence, run_resolution_slice
from services.upkeep.events_maintenance.rollup import compute_rollup
from services.upkeep.events_maintenance.telemetry_replay import recover_telemetry_events
from services.upkeep.events_maintenance.token_totals import fold_totals

_log = logging.getLogger("services.upkeep.events_maintenance.daemon")


def events_maintenance_config() -> EventsMaintenanceConfig:
    """The composition root: the one place this package reads `settings`."""
    return EventsMaintenanceConfig(
        events_maintenance_interval_seconds=settings.daemon.events_maintenance_interval_seconds,
        events_maintenance_pass_deadline_s=settings.daemon.events_maintenance_pass_deadline_s,
        events_maintenance_resolution_deadline_s=settings.daemon.events_maintenance_resolution_deadline_s,
        events_resolution_burst_threshold=settings.daemon.events_resolution_burst_threshold,
        events_resolution_interval_seconds=settings.daemon.events_resolution_interval_seconds,
        events_auto_dismiss_enabled=settings.daemon.events_auto_dismiss_enabled,
        events_auto_dismiss_days=settings.daemon.events_auto_dismiss_days,
        timezone=settings.general.timezone,
        grafana_host=settings.gateway.grafana_host,
        grafana_port=settings.gateway.grafana_port,
        grafana_admin_password=settings.alerts.grafana_admin_password,
    )


def events_maintenance_db(*, gate: ProcessDbGate) -> Database:
    """The handle on the cluster database, built where the daemon (or one of its operator
    commands) starts."""
    return Database.from_settings(gate=gate)


def _endpoint() -> ServiceEndpoint:
    return ServiceEndpoints.from_settings().of("events_maintenance")


def _pidfile() -> Path:
    return _endpoint().pidfile


_LIVENESS_BEAT_STEP_S = 30.0
# One registry-gauge round is a single-row read; a loop that completed nothing for this
# long (three sample intervals) is wedged.
_REGISTRY_GAUGE_LIVENESS_TIMEOUT_S = 180.0
# One reconciliation is a Grafana read (10 s timeout) and one bounded statement.
_ALERT_RECONCILIATION_LIVENESS_TIMEOUT_S = 180.0


class WedgedPassError(RuntimeError):
    """A blocking pass exceeded its deadline and left a worker thread orphaned."""


class _MaintenancePass:
    """One service-owned worker's terminal result, including a late failure."""

    def __init__(
        self, pool: ConnectionPool, name: str, work: Callable[[ConnectionPool], None]
    ) -> None:
        self.pool = pool
        self.name = name
        self.work = work
        self.expired = False

    async def run(self) -> Exception | None:
        """Return the original failure to the caller, or report an expired pass."""
        try:
            await asyncio.to_thread(self.work, self.pool)
        except Exception as exc:
            # A completed pass's caller owns retry/schema-drift policy. Once its
            # deadline expires that caller parks, so this terminal boundary owns
            # the late traceback. The loop remains permanently unhealthy.
            if self.expired:
                _log.exception(
                    "[events-maintenance] %s pass failed after its hard deadline", self.name
                )
            return exc
        return None


def _run_maintenance(
    pool: ConnectionPool, progress: LoopProgress, config: EventsMaintenanceConfig, db: Database
) -> None:
    """One hourly pass: the recoveries (observed metrics, the telemetry mirror replay), the
    cost-ledger rollup of the last closed days (`telemetry_events` → `agent_model_tokens_daily`
    and `agent_metrics_daily`) and the fold of its settled days into `agent_model_tokens_total`, the hourly checkpoint size/row-count telemetry sample, and the
    blob VACUUM. One `now` drives the time-based steps.
    Logs what each step did; a no-op pass logs nothing."""
    now = datetime.now(tz=UTC)
    # Recovery is independent of the rollup: a failing source must not prevent the
    # remaining maintenance work from making progress.
    try:
        with pool.connection() as conn:
            recovered = recover_observations(conn, now=now)
        if recovered:
            _log.info("[events-maintenance] recovered %d metric observations", recovered)
    except Exception:
        _log.exception("[events-maintenance] observed metrics recovery incomplete")
    progress.beat()
    # The telemetry_events record is independent too: its writer reports its own failures and
    # the mirror holds what it missed.
    try:
        with pool.connection() as conn:
            replayed = recover_telemetry_events(conn)
        if replayed:
            _log.info("[events-maintenance] replayed %d telemetry events from the mirror", replayed)
    except Exception:
        _log.exception("[events-maintenance] telemetry events replay incomplete")
    progress.beat()
    with pool.connection() as conn:
        result = compute_rollup(conn, now_utc=now)
        folded = fold_totals(conn, today=now.date())
    progress.beat()
    if folded:
        _log.info("[events-maintenance] folded settled ledger days into %d agent totals", folded)
    if result.start_day is not None:
        _log.info(
            "[events-maintenance] rolled %s..%s — %d metric rows, %d token rows",
            result.start_day,
            result.end_day,
            result.metrics_rows,
            result.tokens_rows,
        )
    # Hourly checkpoint size/row-count sample: the gauge is also emitted after
    # each blob vacuum, but the vacuum only runs inside the 05:00-08:00
    # cluster-time window — emitting here on every pass keeps the series dense
    # enough for growth-curve and rate queries the rest of the day.
    with pool.connection() as conn, conn.cursor() as cur:
        emit_checkpoint_table_sizes(cur)
    progress.beat()
    # Incremental physical reclamation: a plain VACUUM (no lock) over the
    # checkpoint tables, only inside the measured agent-lowest window
    # (05:00-08:00 CLUSTER time). Logs size + dead tuples each run.
    vacuum_result = run_blob_vacuum(db, timezone=config.timezone)
    progress.beat()
    if vacuum_result.ran:
        _log.info("[events-maintenance] blob vacuum: %s", vacuum_result.summary())
    progress.mark_success()


def _run_resolution(
    pool: ConnectionPool,
    progress: LoopProgress,
    config: EventsMaintenanceConfig,
    cadence: AutoDismissCadence,
) -> None:
    """Run the resolution slice while discarding its test-facing summary."""

    run_resolution_slice(pool, config, cadence=cadence)
    progress.beat()
    progress.mark_success()


def _write_pidfile() -> None:
    if not acquire_pidfile(_pidfile(), "services.upkeep.events_maintenance.daemon"):
        _log.info("[events_maintenance] daemon already running (pidfile=%s), exiting", _pidfile())
        sys.exit(1)


def _remove_pidfile() -> None:
    remove_pidfile(_pidfile())


def _is_running() -> bool:
    """Whether a daemon is already running (via its pidfile).

    Pid-reuse-safe: a live pid whose argv does not name this daemon's module
    is a recycled pid, not a running instance (audit round 2, P1)."""
    return pidfile_holds_daemon(_pidfile(), "services.upkeep.events_maintenance.daemon")


def _loop_components(liveness: LivenessGroup) -> list[dict[str, object]]:
    """Project each registered loop tracker into the shared health envelope."""
    now = time.time()
    records: list[dict[str, object]] = []
    for progress in liveness._loops.values():
        snapshot = progress.snapshot()
        stale_for = cast(float, snapshot["stale_for"])
        wedged = cast(bool, snapshot["wedged"])
        last_success_at = cast(str | None, snapshot["last_success_at"])
        last_success = (
            datetime.fromisoformat(last_success_at).timestamp()
            if last_success_at is not None
            else None
        )
        error = cast(dict[str, str] | None, snapshot["last_error"])
        last_error = error["message"] if error is not None else None
        stale = stale_for > progress.timeout_s
        if wedged:
            detail = f"wedged: {last_error or 'pass exceeded its deadline'}"
        elif stale:
            detail = f"no progress for {stale_for:.0f}s"
        else:
            detail = None
        records.append(
            component(
                progress.name,
                DEGRADED if wedged or stale else OK,
                last_success=last_success,
                last_error=last_error,
                progress="wedged" if wedged else "idle",
                detail=detail,
                now=now,
            )
        )
    return records


async def _sleep_with_liveness(progress: LoopProgress, total_s: float) -> None:
    """Sleep `total_s`, beating progress every `_LIVENESS_BEAT_STEP_S` so the long
    inter-poll wait keeps /healthz fresh instead of reading as a wedged loop."""
    remaining = total_s
    while remaining > 0:
        progress.beat()
        step = min(_LIVENESS_BEAT_STEP_S, remaining)
        await asyncio.sleep(step)
        remaining -= step


async def _maintenance_with_liveness(
    pool: ConnectionPool,
    progress: LoopProgress,
    run: Callable[[ConnectionPool], None],
    *,
    tasks: asyncio.TaskGroup,
) -> None:
    """Run one pass (`run`, handed the pool) within its deadline; unresolved work is
    not progress. Completion beats before propagating its result; timeout
    permanently fails the loop.
    """
    worker = _MaintenancePass(pool, progress.name, run)
    fut = tasks.create_task(worker.run())
    done, _pending = await asyncio.wait({fut}, timeout=progress.timeout_s)
    if fut not in done:
        worker.expired = True
        message = f"{progress.name} pass exceeded hard deadline of {progress.timeout_s:.1f}s"
        progress.fail(message)
        raise WedgedPassError(message)
    progress.beat()
    failure = fut.result()
    if failure is not None:
        raise failure


async def _dispatch_loop(
    pool: ConnectionPool,
    progress: LoopProgress,
    config: EventsMaintenanceConfig,
    db: Database,
    *,
    tasks: asyncio.TaskGroup,
) -> None:
    """Main loop: roll immediately on start (fresh after a restart), then every
    interval. The rollup DB work is synchronous psycopg run in a thread so it does
    not block the healthz event loop. The inter-run sleep is OUTSIDE the try, so a
    transient failure waits a full interval before retrying instead of hot-looping
    against Postgres (the rollup is idempotent and self-catching-up — the next run
    re-probes dirty days — so there is no value in an immediate retry)."""
    interval = config.events_maintenance_interval_seconds
    _log.info(
        "[events-maintenance] daemon started, pid=%s, interval=%.0fs",
        os.getpid(),
        interval,
    )
    while True:
        try:
            if not admission.quiesced():
                await _maintenance_with_liveness(
                    pool,
                    progress,
                    lambda target_pool: _run_maintenance(target_pool, progress, config, db),
                    tasks=tasks,
                )
        except asyncio.CancelledError:
            raise
        except WedgedPassError:
            _log.critical(
                "[events-maintenance] rollup pass wedged — parking for watchdog respawn",
                exc_info=True,
            )
            break
        except psycopg.ProgrammingError:
            _log.critical(
                "[events-maintenance] schema / syntax error — code<->DB drift; "
                "retry will not self-heal, daemon exiting, restart after fix",
                exc_info=True,
            )
            raise
        except Exception as exc:
            progress.mark_error(str(exc))
            _log.exception("[events-maintenance] rollup iteration failed")
        await _sleep_with_liveness(progress, interval)


async def _resolution_loop(
    pool: ConnectionPool,
    progress: LoopProgress,
    config: EventsMaintenanceConfig,
    *,
    tasks: asyncio.TaskGroup,
) -> None:
    """Refresh immutable-event class-resolution gauges on their own cadence.

    The six-hour class count and safety-valve write are independent of the hourly
    rollup pass. As with the rollup loop, a transient backend outage waits one full
    configured interval; schema drift exits for watchdog recovery.
    """

    cadence = AutoDismissCadence()
    interval = config.events_resolution_interval_seconds
    _log.info(
        "[events-maintenance] resolution loop started, pid=%s, interval=%ds",
        os.getpid(),
        interval,
    )
    while True:
        try:
            if not admission.quiesced():
                await _maintenance_with_liveness(
                    pool,
                    progress,
                    lambda target_pool: _run_resolution(target_pool, progress, config, cadence),
                    tasks=tasks,
                )
        except asyncio.CancelledError:
            raise
        except WedgedPassError:
            _log.critical(
                "[events-maintenance] resolution pass wedged — parking for watchdog respawn",
                exc_info=True,
            )
            break
        except psycopg.ProgrammingError:
            _log.critical(
                "[events-maintenance] resolution schema / syntax error — "
                "code<->DB drift; retry will not self-heal, restart after fix",
                exc_info=True,
            )
            raise
        except Exception as exc:
            progress.mark_error(str(exc))
            _log.exception("[events-maintenance] resolution iteration failed")
        await _sleep_with_liveness(progress, interval)


async def run(*, database: Callable[[], Database], image: LoadedCommit) -> None:
    """Start healthz, register per-loop progress, then enter all resident loops."""
    if _is_running():
        _log.info(
            "[events-maintenance] daemon already running (pidfile=%s), exiting",
            _pidfile(),
        )
        sys.exit(1)

    # Publish the pidfile before binding healthz so identity-aware probes can verify it.
    _write_pidfile()
    _log.info("[events-maintenance] pidfile written: %s", _pidfile())

    config = events_maintenance_config()
    liveness = LivenessGroup()
    dispatch_progress = liveness.register("dispatch", config.events_maintenance_pass_deadline_s)
    resolution_progress = liveness.register(
        "resolution", config.events_maintenance_resolution_deadline_s
    )
    gauge_progress = liveness.register("registry_gauge", _REGISTRY_GAUGE_LIVENESS_TIMEOUT_S)
    endpoint = _endpoint()
    # Only a unit holding the Grafana credential reconciles alerts; one without it
    # carries no such loop (and no tracker to read as idle).
    reconcile_alerts = alert_reconciler.grafana_reconciliation_configured(config)
    alert_progress = (
        liveness.register("alert_reconciliation", _ALERT_RECONCILIATION_LIVENESS_TIMEOUT_S)
        if reconcile_alerts
        else None
    )
    health = await start_health_server(
        "events_maintenance",
        endpoint.health_port,
        liveness=liveness,
        components=lambda: _loop_components(liveness),
        image=image,
    )
    _log.info("[events-maintenance] healthz listening on :%s", endpoint.health_port)

    db = database()
    pool = db.pool()
    try:
        # The service TaskGroup owns resident loops and their in-flight passes.
        # An expired pass remains owned while healthz asks the watchdog to replace
        # the process. Stop cancels its async proxy; main's hard exit never joins
        # the blocking executor thread.
        async with asyncio.TaskGroup() as loops:
            loops.create_task(_dispatch_loop(pool, dispatch_progress, config, db, tasks=loops))
            loops.create_task(_resolution_loop(pool, resolution_progress, config, tasks=loops))
            loops.create_task(registry_gauge.registry_gauge_loop(pool, gauge_progress))
            if alert_progress is not None:
                bus = EventBus.from_settings()
                loops.create_task(
                    alert_reconciler.reconciliation_loop(pool, bus, alert_progress, config)
                )
    finally:
        pool.close()
        await stop_health_server(health)
        _remove_pidfile()
        _log.info("[events-maintenance] daemon stopped")


def main() -> None:
    """Entry point: init logger + run asyncio loop.

    SIGTERM (the graceful stop the fleet update sends) and Ctrl-C converge on
    the same `KeyboardInterrupt` unwind — see `base.daemon.shutdown`. `ava stop`
    default force-kill does not reach this.
    """
    from base.deploy.schema.migrations import assert_schema_current

    image = LoadedCommit.capture()
    # Pre-startup sanity: schema version must match code; raises SchemaVersionMismatch if not.
    assert_schema_current(settings.data_plane.db_url)
    version = CodeVersion(image)
    gate = ProcessDbGate(version=version.get, process="events_maintenance")

    def database() -> Database:
        return Database.from_settings(gate=gate)

    pipeline = build_pipeline(database=database)
    init_gateway_process(
        name="events_maintenance",
        producer=lambda: pipeline,
        machine_reader=lambda: validate_machine_name(settings.general.machine_name),
        image=image,
    )
    install_graceful_shutdown("events_maintenance")
    code = 0
    # `asyncio.Runner`, not `asyncio.run`: `run` closes in a `finally` that
    # awaits `shutdown_default_executor`, joining the default executor's
    # workers — an in-flight maintenance pass among them — and a stop signal
    # must never wait on those (see `_hard_exit`). The runner is therefore
    # never closed: after the explicit drain below, teardown is skipped by the
    # hard exit.
    runner = asyncio.Runner()
    try:
        runner.run(run(database=database, image=image))
    except KeyboardInterrupt:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)  # a retry must not abort the bounded exit
        _log.info("[events-maintenance] interrupted, shutting down")
        # The signal path skips Runner's own cancellation, so drain the loop's
        # tasks explicitly: run()'s finally still closes the pool, stops the
        # health server and removes the pidfile. The executor is deliberately
        # NOT drained.
        failures = cancel_and_drain(runner)
        if failures:
            _log.error("[events-maintenance] async shutdown failed: %r", failures)
            code = 1
    except Exception:
        _log.exception("[events-maintenance] daemon crashed — uncaught exception escaped run()")
        code = 1
    finally:
        _remove_pidfile()
    _hard_exit(code)


if __name__ == "__main__":
    main()
