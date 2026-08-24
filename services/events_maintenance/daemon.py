"""Events-maintenance daemon — gateway-owned unified event-stream + checkpoint maintenance.

Always-on gateway daemon (cluster-wide; the gateway owns the data plane). Two
loops:

- Hourly loop (`AVA_EVENTS_MAINTENANCE_INTERVAL_SECONDS`, default 1h): the
  events-archive slices — ensure the current + next UTC-month partitions exist
  for the unified `events` stream (`services.events_maintenance.partitions`),
  apply the retention DROP/prune of expired months
  (`services.events_maintenance.retention`), run the table-retention pass for
  the append-only fact tables (`services.events_maintenance.table_retention`),
  recompute the day-grain rollup tables
  (`services.events_maintenance.rollup`), run the index-bloat governance
  (`services.events_maintenance.reindex`) — PLUS, unconditionally, the
  Rule B checkpoint reaper
  (`services.events_maintenance.checkpoint_reaper`, Task #1057): terminated /
  inactive agents' checkpoints are trimmed to keep=1 (the PostgresSaver is
  append-only, so terminated threads grow without bound — the 2026-08-08 disk
  crisis: 21GB of blobs; the surviving latest checkpoint keeps a resurrect
  fully restorable) and the incremental blob VACUUM
  (`services.events_maintenance.blob_vacuum`).
- Fast loop (60s): Rule A — trim overgrown LIVE threads
  (`services.events_maintenance.checkpoint_reaper.trim_overgrown_threads`,
  keep=2 past 20), replacing the removed agent-side idle trim.

The events-archive slices only run when `AVA_EVENTS_MAINTENANCE_ENABLED` is
set. Since the LGTM cutover (task #1197) the PG `events` copy is a read-only
archive — nothing writes or reads it — so the flag defaults off and those
slices are no-ops on every cluster. The checkpoint slices are NOT gated:
checkpoint_blobs growth is independent of the events pipeline, and the daemon
must keep reaping even when the archive slices are off (the 2026-08-12 design
regression: gating the whole daemon off stopped the reaper and checkpoint_blobs
grew ~150MB/h unbounded).

The retention slices DO delete data — that is their job; they never touch the
legacy `agent_events` / `event_log` tables (frozen since the migration window
closed; their removal is a separate migration-cleanup task).

The rollup only covers whole days up to yesterday (UTC); today is served live by
the readers. The upsert is a full-day overwrite recompute keyed on the PK, so
re-running is idempotent — a restart or a fast interval never double-counts.
An indexed slice with zero aggregate rows is warned and skipped rather than
treated as an empty day, leaving that day's existing ledger rows intact.
Partition creation is likewise idempotent (an already-covered month is a no-op).

Usage:
    .venv/bin/python -m services.events_maintenance.daemon

Kept alive by the gateway watchdog's 60s healthcheck
(`services.healthchecks.events_maintenance`), so the schema-drift exit in
`_dispatch_loop` is revived on the next round instead of staying dead.
"""

import asyncio
import logging
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime

import psycopg
from psycopg_pool import ConnectionPool

import shared.db
from services._pidfile import acquire_pidfile, pidfile_holds_daemon, remove_pidfile
from services.events_maintenance.blob_vacuum import run_blob_vacuum
from services.events_maintenance.checkpoint_reaper import (
    reap_stale_checkpoints,
    trim_overgrown_threads,
)
from services.events_maintenance.partitions import ensure_month_partitions
from services.events_maintenance.reindex import run_governance_pass
from services.events_maintenance.resolution import run_resolution_slice
from services.events_maintenance.retention import apply_retention
from services.events_maintenance.rollup import compute_rollup
from services.events_maintenance.table_retention import apply_table_retention
from shared.config import settings
from shared.daemon_health import Liveness, health_port, start_health_server, stop_health_server
from shared.daemon_shutdown import install_graceful_shutdown
from shared.events.contract import EVENTS, RETENTION_BY_CATEGORY, retention_days
from shared.log import init_gateway_process
from shared.paths import legacy_pid_path

_log = logging.getLogger("services.events_maintenance.daemon")

_PIDFILE = settings.services.events_maintenance_pidfile
_LIVENESS_TIMEOUT_S = 60.0
_LIVENESS_BEAT_STEP_S = 30.0

# Cadence of the Rule A fast loop. #1197 removed the ops-rollup fast loop this
# pass rode on, silently killing Rule A (trim_overgrown_threads became dead
# code); it gets its own loop so a continuously working agent's checkpoints are
# bounded within a minute of crossing the threshold.
_CHECKPOINT_TRIM_INTERVAL_S = 60.0


def _retention_policy() -> dict[str, int]:
    """event_name -> retention days for the retention slice, derived from the
    event registry (shared/events/contract.py) — the single source of truth:
    each event's `EventSpec.retention_days` override, else its category floor
    (audit 365d / telemetry 90d / log 30d). The settings knobs
    (AVA_EVENTS_RETENTION_*_DAYS) are optional per-category overrides (None =
    registry default), applied only to events without an explicit per-event
    override, so changing the registry changes the docs and the daemon's
    behavior together (R2: one fact, one declaration)."""
    policy = {name: retention_days(name) for name in EVENTS}
    for category in RETENTION_BY_CATEGORY:
        override = getattr(settings.daemon, f"events_retention_{category}_days")
        if override is None:
            continue
        for name, spec in EVENTS.items():
            if spec.category == category and spec.retention_days is None:
                policy[name] = override
    return policy


def _run_maintenance(pool: ConnectionPool) -> None:
    """One hourly pass. The events-archive slices — partition rolling, events
    retention (drop/prune expired months), table retention, index-bloat
    governance (audit M2) — run only when
    `AVA_EVENTS_MAINTENANCE_ENABLED` is set; since the LGTM cutover (task
    #1197) the PG `events` copy is a read-only archive that nothing writes or
    reads, so those slices are dead weight on every cluster. Three steps are
    NOT gated: the cost-ledger rollup (Loki → `agent_model_tokens_daily` —
    Loki only retains 168h, so skipping passes permanently loses days), the
    Rule B checkpoint reaper (terminated / inactive agents to keep=1), and the
    blob VACUUM — checkpoint_blobs growth is independent of the events
    pipeline, and the reaper is the only thing that bounds it (gating the
    daemon off, the 2026-08-12 design regression, let blobs grow ~150MB/h
    unbounded). One `now` drives the time-based steps.
    Logs what each step did; a no-op pass logs nothing.

    Each step runs on a SEPARATE pool connection so the partition DDL commits
    (and releases its locks) before the potentially long rollup starts —
    sharing one non-autocommit connection would hold the CREATE PARTITION /
    DROP PARTITION locks through the whole rollup (blocking event-table
    writes) and let a rollup failure roll back the just-created partitions. The
    retention step runs AFTER partition creation so months stranded in DEFAULT
    are first carved into their own partitions and then dropped/pruned like any
    other month."""
    now = datetime.now(tz=UTC)
    if settings.daemon.events_maintenance_enabled:
        with pool.connection() as conn:
            created = ensure_month_partitions(conn, now_utc=now)
        with pool.connection() as conn:
            retention = apply_retention(conn, now_utc=now, retention_days=_retention_policy())
        with pool.connection() as conn:
            table_retention = apply_table_retention(conn, now_utc=now)
        # Index-bloat governance (audit M2 / P1-2 ①): REINDEX CONCURRENTLY the hot
        # events partition indexes past their bytes/row threshold. Runs LAST and on
        # its own DIRECT autocommit connection — CONCURRENTLY cannot run in a
        # transaction and must not ride the transaction-mode pooler. A no-op pass
        # (healthy indexes) logs nothing.
        reindex_result = run_governance_pass(now_utc=now)
        if created:
            _log.info("[events-maintenance] created partitions: %s", ", ".join(created))
        if retention.dropped or retention.pruned:
            _log.info("[events-maintenance] retention: %s", retention.summary())
        if table_retention.deleted:
            _log.info("[events-maintenance] table retention: %s", table_retention.summary())
        if reindex_result.summary():
            _log.info("[events-maintenance] index governance: %s", reindex_result.summary())
    with pool.connection() as conn:
        result = compute_rollup(conn, now_utc=now)
    if result.start_day is not None:
        _log.info(
            "[events-maintenance] rolled %s..%s — %d metric rows, %d token rows",
            result.start_day,
            result.end_day,
            result.metrics_rows,
            result.tokens_rows,
        )
    reaped = reap_stale_checkpoints(pool)
    # Incremental physical reclamation: a plain VACUUM (no lock) over the
    # checkpoint tables, only inside the measured agent-lowest window
    # (05:00-08:00 America/Los_Angeles). Logs size + dead tuples each run.
    vacuum_result = run_blob_vacuum()
    if vacuum_result.ran:
        _log.info("[events-maintenance] blob vacuum: %s", vacuum_result.summary())
    if reaped.agents:
        _log.info(
            "[events-maintenance] checkpoint reaper: %d stale agent(s), "
            "%d checkpoints / %d writes / %d blobs",
            reaped.agents,
            reaped.checkpoints,
            reaped.writes,
            reaped.blobs,
        )


def _run_checkpoint_trim(pool: ConnectionPool) -> None:
    """One fast checkpoint-trim pass: Rule A — trim active threads past
    `_LIVE_TRIM_THRESHOLD` checkpoints down to `_LIVE_KEEP` (compaction
    boundaries always kept). Replaces the removed agent-side idle trim, which
    only fired when an agent actually idled; the service bounds every thread
    regardless of liveness. Runs on the fast loop so a continuously working
    agent's checkpoints are bounded within one loop interval."""
    now = datetime.now(tz=UTC)
    reaped = trim_overgrown_threads(pool)
    if reaped.agents:
        _log.info(
            "[events-maintenance] checkpoint trim (fast): %d overgrown thread(s), "
            "%d checkpoints / %d writes / %d blobs (t=%.0fs)",
            reaped.agents,
            reaped.checkpoints,
            reaped.writes,
            reaped.blobs,
            now.timestamp(),
        )


def _run_resolution(pool: ConnectionPool) -> None:
    """Run the resolution slice while discarding its test-facing summary."""

    run_resolution_slice(pool)


def _write_pidfile() -> None:
    if not acquire_pidfile(_PIDFILE, "services.events_maintenance.daemon"):
        _log.info("[events_maintenance] daemon already running (pidfile=%s), exiting", _PIDFILE)
        sys.exit(1)


def _remove_pidfile() -> None:
    remove_pidfile(_PIDFILE)


def _is_running() -> bool:
    """Whether a daemon is already running (via pidfile, new + legacy paths).

    Pid-reuse-safe: a live pid whose argv does not name this daemon's module
    is a recycled pid, not a running instance (audit round 2, P1)."""
    return pidfile_holds_daemon(
        _PIDFILE, "services.events_maintenance.daemon"
    ) or pidfile_holds_daemon(
        legacy_pid_path("events_maintenance"), "services.events_maintenance.daemon"
    )


async def _sleep_with_liveness(liveness: Liveness, total_s: float) -> None:
    """Sleep `total_s`, beating liveness every `_LIVENESS_BEAT_STEP_S` so the long
    inter-poll wait keeps /healthz fresh instead of reading as a wedged loop."""
    remaining = total_s
    while remaining > 0:
        liveness.beat()
        step = min(_LIVENESS_BEAT_STEP_S, remaining)
        await asyncio.sleep(step)
        remaining -= step


async def _maintenance_with_liveness(
    pool: ConnectionPool, liveness: Liveness, *, run: Callable[[ConnectionPool], None] | None = None
) -> None:
    """Run the (blocking) maintenance pass in a worker thread while beating liveness
    every `_LIVENESS_BEAT_STEP_S`. The first run's full-history backfill can outlast
    `_LIVENESS_TIMEOUT_S`; without beating during it, /healthz would flip 503 and
    the watchdog would respawn the daemon mid-scan. Re-raises the pass's own
    exception once the thread finishes. `run` defaults to `_run_maintenance`,
    resolved at call time (not bound as a default) so tests can monkeypatch it."""
    fut = asyncio.ensure_future(asyncio.to_thread(run or _run_maintenance, pool))
    while True:
        liveness.beat()
        done, _pending = await asyncio.wait({fut}, timeout=_LIVENESS_BEAT_STEP_S)
        if fut in done:
            break
    await fut  # propagate the maintenance pass's exception, if any


async def _dispatch_loop(pool: ConnectionPool, liveness: Liveness) -> None:
    """Main loop: roll immediately on start (fresh after a restart), then every
    interval. The rollup DB work is synchronous psycopg run in a thread so it does
    not block the healthz event loop. The inter-run sleep is OUTSIDE the try, so a
    transient failure waits a full interval before retrying instead of hot-looping
    against Postgres (the rollup is idempotent and self-catching-up — the next run
    re-probes dirty days — so there is no value in an immediate retry)."""
    interval = settings.daemon.events_maintenance_interval_seconds
    _log.info(
        "[events-maintenance] daemon started, pid=%s, interval=%.0fs",
        os.getpid(),
        interval,
    )
    while True:
        try:
            await _maintenance_with_liveness(pool, liveness)
        except asyncio.CancelledError:
            raise
        except psycopg.ProgrammingError:
            _log.critical(
                "[events-maintenance] schema / syntax error — code<->DB drift; "
                "retry will not self-heal, daemon exiting, restart after fix",
                exc_info=True,
            )
            raise
        except Exception:
            _log.exception("[events-maintenance] rollup iteration failed")
        await _sleep_with_liveness(liveness, interval)


async def _checkpoint_trim_loop(pool: ConnectionPool, liveness: Liveness) -> None:
    """Rule A fast loop: trim overgrown live threads every
    `_CHECKPOINT_TRIM_INTERVAL_S`. Runs unconditionally (independent of
    `AVA_EVENTS_MAINTENANCE_ENABLED`) — the hourly pass only bounds stale
    threads, and a continuously working agent would grow without bound between
    hourly passes. Same failure posture as `_dispatch_loop`: a transient error
    waits a full interval (the trim is idempotent and self-catching-up), a
    schema/syntax error exits the daemon so the watchdog revives it after the
    fix."""
    _log.info(
        "[events-maintenance] checkpoint trim loop started, pid=%s, interval=%.0fs",
        os.getpid(),
        _CHECKPOINT_TRIM_INTERVAL_S,
    )
    while True:
        try:
            await _maintenance_with_liveness(pool, liveness, run=_run_checkpoint_trim)
        except asyncio.CancelledError:
            raise
        except psycopg.ProgrammingError:
            _log.critical(
                "[events-maintenance] checkpoint trim schema / syntax error — "
                "code<->DB drift; retry will not self-heal, daemon exiting, "
                "restart after fix",
                exc_info=True,
            )
            raise
        except Exception:
            _log.exception("[events-maintenance] checkpoint trim iteration failed")
        await _sleep_with_liveness(liveness, _CHECKPOINT_TRIM_INTERVAL_S)


async def _resolution_loop(pool: ConnectionPool, liveness: Liveness) -> None:
    """Refresh immutable-event class-resolution gauges on their own cadence.

    The six-hour Loki read and safety-valve write are unrelated to the frozen
    archive's hourly rollup and must run while archive maintenance is disabled.
    As with the checkpoint loops, a transient backend outage waits one full
    configured interval; schema drift exits for watchdog recovery.
    """

    interval = settings.daemon.events_resolution_interval_seconds
    _log.info(
        "[events-maintenance] resolution loop started, pid=%s, interval=%ds",
        os.getpid(),
        interval,
    )
    while True:
        try:
            await _maintenance_with_liveness(pool, liveness, run=_run_resolution)
        except asyncio.CancelledError:
            raise
        except psycopg.ProgrammingError:
            _log.critical(
                "[events-maintenance] resolution schema / syntax error — "
                "code<->DB drift; retry will not self-heal, restart after fix",
                exc_info=True,
            )
            raise
        except Exception:
            _log.exception("[events-maintenance] resolution iteration failed")
        await _sleep_with_liveness(liveness, interval)


async def run() -> None:
    """Start the daemon: healthz server -> write pidfile -> connect DB -> enter main loop."""
    if _is_running():
        _log.info(
            "[events-maintenance] daemon already running (pidfile=%s), exiting",
            _PIDFILE,
        )
        sys.exit(1)

    # Pidfile before the healthz bind — see services/restarter/daemon.py:run().
    _write_pidfile()
    _log.info("[events-maintenance] pidfile written: %s", _PIDFILE)

    liveness = Liveness(_LIVENESS_TIMEOUT_S)
    health = await start_health_server("events_maintenance", liveness=liveness)
    _log.info("[events-maintenance] healthz listening on :%s", health_port("events_maintenance"))

    pool = shared.db.pool()
    try:
        await asyncio.gather(
            _dispatch_loop(pool, liveness),
            _checkpoint_trim_loop(pool, liveness),
            _resolution_loop(pool, liveness),
        )
    finally:
        pool.close()
        await stop_health_server(health)
        _remove_pidfile()
        _log.info("[events-maintenance] daemon stopped")


def main() -> None:
    """Entry point: init logger + run asyncio loop.

    SIGTERM (the graceful stop `ava cluster update` sends) and Ctrl-C converge on
    the same `KeyboardInterrupt` unwind — see `shared.daemon_shutdown`. `ava stop`
    default force-kill does not reach this.
    """
    from shared.migrations import assert_schema_current

    # Pre-startup sanity: schema version must match code; raises SchemaVersionMismatch if not.
    assert_schema_current(settings.data_plane.db_url)
    init_gateway_process(name="events_maintenance")
    install_graceful_shutdown("events_maintenance")
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        _log.info("[events-maintenance] interrupted, shutting down")
    except Exception:
        _log.exception("[events-maintenance] daemon crashed — uncaught exception escaped run()")
        raise
    finally:
        _remove_pidfile()


if __name__ == "__main__":
    main()
