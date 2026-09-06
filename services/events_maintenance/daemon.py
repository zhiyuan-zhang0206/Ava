"""Events-maintenance daemon — gateway-owned unified event-stream + checkpoint maintenance.

Always-on gateway daemon (cluster-wide; the gateway owns the data plane). Three
loops:

- Hourly loop (`AVA_EVENTS_MAINTENANCE_INTERVAL_SECONDS`, default 1h): recompute
  the day-grain rollup tables (`services.events_maintenance.rollup`), replay
  ledger gaps from the retained JSONL mirror
  (`services.events_maintenance.jsonl_replay`), run the incremental blob VACUUM
  (`services.events_maintenance.blob_vacuum`), and the hourly checkpoint
  size/row-count telemetry sample. The PG `events` archive slices
  (partitions / retention / table retention / index governance) were removed
  with the task #1281/#1823 cleanup — the table was dropped and its data lives
  in the Loki archive stream.
- Fast loop (60s): prune every checkpoint thread above three rows to its newest
  three (`services.events_maintenance.checkpoint_reaper.prune_threads`).
- Resolution loop (`AVA_EVENTS_RESOLUTION_INTERVAL_SECONDS`, default 5m):
  refresh immutable-event class-resolution state and gauges from Loki.

Each loop reports independent progress, success, and errors in `/healthz`.
Only completed bounded work or sleeps beat; exceeding a hard deadline fails
healthz until the watchdog replaces the process and its orphaned worker thread.
The same trackers are projected as unified envelope components, so the legacy
per-loop snapshots and the component degradation reasons describe one state.

The PG `events` archive slices (partitions / retention / table retention /
index governance) were removed with the task #1281/#1823 cleanup — the frozen
archive was dropped and its rows live in the Loki archive stream. The
checkpoint slices are NOT gated: checkpoint_blobs growth is independent of the
events pipeline, and the daemon must keep reaping (the 2026-08-12 design
regression: gating the whole daemon off stopped the reaper and checkpoint_blobs
grew ~150MB/h unbounded).

The rollup only covers whole days up to yesterday (UTC); today is served live by
the readers. The upsert is a full-day overwrite recompute keyed on the PK, so
re-running is idempotent — a restart or a fast interval never double-counts.
An indexed slice with zero aggregate rows is warned and skipped rather than
treated as an empty day, leaving that day's existing ledger rows intact.

Usage:
    .venv/bin/python -m services.events_maintenance.daemon

Kept alive by the gateway watchdog's 60s healthcheck
(`services.healthchecks.events_maintenance`), so the schema-drift exit in
`_dispatch_loop` is revived on the next round instead of staying dead.

Health reports progress independently for the rollup, checkpoint-trim, and
resolution loops. The endpoint takes the worst state: only a completed bounded
unit refreshes a loop's success timestamp, and a worker running beyond its hard
deadline makes `/healthz` return 503 for watchdog recovery.
"""

import asyncio
import logging
import os
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import cast

import psycopg
from psycopg_pool import ConnectionPool

import shared.db
from services._pidfile import acquire_pidfile, pidfile_holds_daemon, remove_pidfile
from services.events_maintenance.blob_vacuum import (
    emit_checkpoint_table_sizes,
    run_blob_vacuum,
)
from services.events_maintenance.checkpoint_reaper import prune_threads
from services.events_maintenance.jsonl_replay import replay_gap_days
from services.events_maintenance.resolution import run_resolution_slice
from services.events_maintenance.rollup import compute_rollup
from shared.config import settings
from shared.daemon_health import (
    LivenessGroup,
    LoopProgress,
    health_port,
    start_health_server,
    stop_health_server,
)
from shared.daemon_shutdown import install_graceful_shutdown
from shared.health_schema import DEGRADED, OK, component
from shared.log import init_gateway_process
from shared.paths import legacy_pid_path

_log = logging.getLogger("services.events_maintenance.daemon")

_PIDFILE = settings.services.events_maintenance_pidfile
_LIVENESS_BEAT_STEP_S = 30.0

# The fixed per-thread checkpoint budget is enforced within one minute of a
# thread crossing it, independent of agent liveness or the hourly event work.
_CHECKPOINT_TRIM_INTERVAL_S = 60.0


class WedgedPassError(RuntimeError):
    """A blocking pass exceeded its deadline and left a worker thread orphaned."""


def _run_maintenance(pool: ConnectionPool, progress: LoopProgress) -> None:
    """One hourly pass: the cost-ledger rollup (Loki → `agent_model_tokens_daily`
    — Loki only retains 84h, so skipping passes permanently loses days), the
    JSONL gap replay, the hourly checkpoint size/row-count telemetry sample,
    and the blob VACUUM. One `now` drives the time-based steps.
    Logs what each step did; a no-op pass logs nothing."""
    now = datetime.now(tz=UTC)
    with pool.connection() as conn:
        result = compute_rollup(conn, now_utc=now)
        replay_result = replay_gap_days(conn, now_utc=now)
    progress.beat()
    if result.start_day is not None:
        _log.info(
            "[events-maintenance] rolled %s..%s — %d metric rows, %d token rows",
            result.start_day,
            result.end_day,
            result.metrics_rows,
            result.tokens_rows,
        )
    if replay_result.days_replayed or replay_result.days_failed:
        _log.info(
            "[events-maintenance] JSONL replay — replayed=%s failed=%s, "
            "%d metric rows, %d token rows",
            replay_result.days_replayed,
            replay_result.days_failed,
            replay_result.metrics_rows,
            replay_result.tokens_rows,
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
    vacuum_result = run_blob_vacuum()
    progress.beat()
    if vacuum_result.ran:
        _log.info("[events-maintenance] blob vacuum: %s", vacuum_result.summary())
    progress.mark_success()


def _run_checkpoint_trim(pool: ConnectionPool, progress: LoopProgress) -> None:
    """Prune every checkpoint thread to newest-three on the fast loop."""
    pruned = prune_threads(pool)
    if pruned.agents:
        _log.info(
            "[events-maintenance] checkpoint prune: %d thread(s), "
            "%d checkpoints / %d writes / %d blobs",
            pruned.agents,
            pruned.checkpoints,
            pruned.writes,
            pruned.blobs,
        )
    progress.beat()
    progress.mark_success()


def _run_resolution(pool: ConnectionPool, progress: LoopProgress) -> None:
    """Run the resolution slice while discarding its test-facing summary."""

    run_resolution_slice(pool)
    progress.beat()
    progress.mark_success()


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
    *,
    run: Callable[[ConnectionPool], None] | None = None,
) -> None:
    """Run one pass within its deadline; unresolved work is not progress.
    Completion beats before propagating its result; timeout permanently fails
    the loop. ``run`` remains a test seam.
    """
    if run is None:

        def run_with_progress(target_pool: ConnectionPool) -> None:
            _run_maintenance(target_pool, progress)

        run = run_with_progress
    fut = asyncio.ensure_future(asyncio.to_thread(run, pool))
    done, _pending = await asyncio.wait({fut}, timeout=progress.timeout_s)
    if fut not in done:
        message = f"{progress.name} pass exceeded hard deadline of {progress.timeout_s:.1f}s"
        progress.fail(message)
        raise WedgedPassError(message)
    progress.beat()
    await fut  # propagate the completed maintenance pass's exception, if any


async def _dispatch_loop(pool: ConnectionPool, progress: LoopProgress) -> None:
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
            await _maintenance_with_liveness(pool, progress)
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


async def _checkpoint_trim_loop(pool: ConnectionPool, progress: LoopProgress) -> None:
    """Prune all over-budget checkpoint threads every fast-loop interval.

    The loop is unconditional and independent of agent liveness. It has the
    same failure posture as `_dispatch_loop`: a transient error waits a full
    interval (the prune is idempotent and self-catching-up), while a schema or
    syntax error exits so the watchdog revives the daemon after the fix.
    """
    _log.info(
        "[events-maintenance] checkpoint trim loop started, pid=%s, interval=%.0fs",
        os.getpid(),
        _CHECKPOINT_TRIM_INTERVAL_S,
    )
    while True:
        try:
            await _maintenance_with_liveness(
                pool,
                progress,
                run=lambda target_pool: _run_checkpoint_trim(target_pool, progress),
            )
        except asyncio.CancelledError:
            raise
        except WedgedPassError:
            _log.critical(
                "[events-maintenance] checkpoint trim pass wedged — parking for watchdog respawn",
                exc_info=True,
            )
            break
        except psycopg.ProgrammingError:
            _log.critical(
                "[events-maintenance] checkpoint trim schema / syntax error — "
                "code<->DB drift; retry will not self-heal, daemon exiting, "
                "restart after fix",
                exc_info=True,
            )
            raise
        except Exception as exc:
            progress.mark_error(str(exc))
            _log.exception("[events-maintenance] checkpoint trim iteration failed")
        await _sleep_with_liveness(progress, _CHECKPOINT_TRIM_INTERVAL_S)


async def _resolution_loop(pool: ConnectionPool, progress: LoopProgress) -> None:
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
            await _maintenance_with_liveness(
                pool,
                progress,
                run=lambda target_pool: _run_resolution(target_pool, progress),
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


async def run() -> None:
    """Start healthz, register per-loop progress, then enter all maintenance loops."""
    if _is_running():
        _log.info(
            "[events-maintenance] daemon already running (pidfile=%s), exiting",
            _PIDFILE,
        )
        sys.exit(1)

    # Pidfile before the healthz bind — see services/restarter/daemon.py:run().
    _write_pidfile()
    _log.info("[events-maintenance] pidfile written: %s", _PIDFILE)

    liveness = LivenessGroup()
    dispatch_progress = liveness.register(
        "dispatch", settings.daemon.events_maintenance_pass_deadline_s
    )
    trim_progress = liveness.register("trim", settings.daemon.events_maintenance_trim_deadline_s)
    resolution_progress = liveness.register(
        "resolution", settings.daemon.events_maintenance_resolution_deadline_s
    )
    health = await start_health_server(
        "events_maintenance",
        liveness=liveness,
        components=lambda: _loop_components(liveness),
    )
    _log.info("[events-maintenance] healthz listening on :%s", health_port("events_maintenance"))

    pool = shared.db.pool()
    try:
        await asyncio.gather(
            _dispatch_loop(pool, dispatch_progress),
            _checkpoint_trim_loop(pool, trim_progress),
            _resolution_loop(pool, resolution_progress),
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
