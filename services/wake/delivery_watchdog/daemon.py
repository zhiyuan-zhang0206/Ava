"""Observe stalled deliveries and retire stale inputs without dispatching work."""

import asyncio
import logging
import signal
import sys
import time
from collections.abc import Callable
from pathlib import Path

from psycopg_pool import ConnectionPool

from base import telemetry
from base.cluster.machine.boot import machine_from_boot
from base.config import ConfigBoot, settings
from base.daemon import round_loop
from base.daemon.endpoints import ServiceEndpoint, ServiceEndpoints
from base.daemon.health import start_health_server, stop_health_server
from base.daemon.loop_health import LivenessGroup, LoopProgress
from base.daemon.shutdown import cancel_and_drain, install_graceful_shutdown
from base.daemon.shutdown import hard_exit as _hard_exit
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.deploy.maintenance import admission
from base.log.process import init_gateway_process
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from base.paths import ava_home
from base.telemetry.emitter import build_pipeline
from services.pidfile import acquire_pidfile, pidfile_holds_daemon, remove_pidfile
from services.wake.delivery_watchdog.dead_letter import (
    dead_letter_stale_claimed as dead_letter_stale_claimed,
)
from services.wake.delivery_watchdog.dead_letter import (
    dead_letter_stale_pending_chats as dead_letter_stale_pending_chats,
)
from services.wake.delivery_watchdog.dead_letter import (
    dead_letter_stale_pending_resurrects as dead_letter_stale_pending_resurrects,
)
from services.wake.delivery_watchdog.dead_letter import (
    dead_letter_stale_pending_terminated as dead_letter_stale_pending_terminated,
)

_log = logging.getLogger("services.wake.delivery_watchdog.daemon")


def _endpoint() -> ServiceEndpoint:
    return ServiceEndpoints.from_settings().of("delivery_watchdog")


def _pidfile() -> Path:
    return _endpoint().pidfile


# Liveness staleness ceiling of the scan loop. It sleeps a short inter-poll
# interval and `round_loop.sleep_with_progress` beats during that wait; the ceiling
# only has to exceed one beat step, not the whole interval.
_SCAN_LIVENESS_TIMEOUT_S = 60.0
# The observer issues one database operation at a time.
_POOL_MAX_SIZE = 1


def select_stale_pending(
    pool: ConnectionPool,
    threshold_s: float,
) -> list[tuple[int, int, str | None, float]]:
    """Chat inbounds still `pending` past `threshold_s`, as
    `(inbound_id, agent_id, agent_label, age_seconds)`, oldest first.

    An owner mid-turn (status='running') queues inbound legitimately — the
    claim's turn-end SELECT picks them up, so they are NOT stalls (a long LLM
    turn with a queued user message is normal). Only waiting/terminal owners signal
    a real stall: 'idling' (lost wake), 'terminated' (delivery auto-resurrect
    failed).
    """
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT m.id, m.agent_id, a.label, "
            "       EXTRACT(EPOCH FROM (now() - m.created_at)) AS age_s "
            "FROM inbound_messages m "
            "LEFT JOIN agents a ON a.id = m.agent_id "
            "JOIN agents_meta am ON am.id = m.agent_id "
            "WHERE m.status = 'pending' AND m.kind = 'chat' "
            "  AND am.status IN ('idling', 'terminated') "
            "  AND m.created_at < now() - make_interval(secs => %s) "
            "ORDER BY m.created_at ASC",
            (threshold_s,),
        )
        return [(r[0], r[1], r[2], float(r[3])) for r in cur.fetchall()]


def select_pending_ids(pool: ConnectionPool) -> set[int]:
    """Every currently-pending inbound id — used to prune the alert set so a
    row that left `pending` stops being remembered (and re-alerts if it ever
    comes back)."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM inbound_messages WHERE status = 'pending'")
        return {r[0] for r in cur.fetchall()}


def scan_once(
    pool: ConnectionPool,
    threshold_s: float,
    alerted: set[int],
) -> tuple[int, set[int]]:
    """One scan: alert each chat inbound stalled past `threshold_s` that is
    not already in `alerted`; prune `alerted` down to rows still pending.

    `alerted` is process-local notification debounce. Returns
    `(newly_alerted_count, updated_alerted_set)` — extracted from the loop so
    the once-per-row-while-stuck semantics are unit-testable without running
    the daemon.
    """
    stale = select_stale_pending(pool, threshold_s)
    newly_alerted = 0
    for inbound_id, agent_id, label, age_s in stale:
        if inbound_id not in alerted:
            _alert_stalled(inbound_id, agent_id, label, age_s)
            alerted.add(inbound_id)
            newly_alerted += 1
    pending = select_pending_ids(pool)
    alerted &= pending
    return newly_alerted, alerted


def _alert_stalled(
    inbound_id: int,
    agent_id: int,
    label: str | None,
    age_s: float,
) -> None:
    """One alert per stalled row, through the unified event alone (it feeds
    the frontend SSE stream / metrics via the event pipeline); the log line is
    the INFO local trace — the pair read as a second send in the 2026-10-03/04
    triage. An emit failure is logged, never raised."""
    _log.info(
        "[delivery] inbound %s to agent %s (%s) still pending after %.0fs — "
        "delivery stalled; pub/sub wake lost or claim not running",
        inbound_id,
        agent_id,
        label or f"#{agent_id}",
        age_s,
    )
    # ts = process clock at enqueue (emitter stamps datetime.now(UTC)) — the
    # old direct agent_events INSERT used DB now(); see services/wake/heartbeat/
    # daemon.py's W7 rewiring note for the same time-source unification.
    try:
        telemetry.emit(
            "telemetry",
            "delivery_stalled",
            level="warning",
            agent_id=agent_id,
            source="system",
            attributes={"inbound_id": inbound_id, "age_s": round(age_s)},
        )
    except Exception:
        _log.exception("[delivery] delivery_stalled emit failed for inbound %s", inbound_id)


def _write_pidfile() -> None:
    if not acquire_pidfile(_pidfile(), "services.wake.delivery_watchdog.daemon"):
        _log.info("[delivery_watchdog] daemon already running (pidfile=%s), exiting", _pidfile())
        sys.exit(1)


def _remove_pidfile() -> None:
    remove_pidfile(_pidfile())


def _is_running() -> bool:
    """Whether a daemon is already running (via its pidfile).

    Pid-reuse-safe: a live pid whose argv does not name this daemon's module
    is a recycled pid, not a running instance (audit round 2, P1)."""
    return pidfile_holds_daemon(_pidfile(), "services.wake.delivery_watchdog.daemon")


# Stale-claimed dead-letter cadence (Task #654): the sweep is time-gated (not
# tick-gated) so its real period is independent of the tick interval. 30s is
# plenty — the hazard is a resurrect re-delivering ancient rows, and a
# resurrect takes seconds to boot, so the sweep is virtually always ahead of
# it; the reconcile-side cutoff (agent/db/__init__.py) closes the residual race.
_CLAIMED_SWEEP_INTERVAL_S = 30.0


def _maybe_sweep_stale_inbounds(
    pool: ConnectionPool,
    threshold_s: float,
    idling_threshold_s: float,
    last_sweep_at: float,
) -> float:
    """Run stale-inbound dead-letter sweeps on the configured cadence.

    Return the new monotonic sweep timestamp. All sweeps are best-effort: a
    failure is logged, never raised, and the next gate window retries.
    """
    now_mono = time.monotonic()
    if now_mono - last_sweep_at < _CLAIMED_SWEEP_INTERVAL_S:
        return last_sweep_at
    try:
        dead_lettered = dead_letter_stale_claimed(pool, threshold_s, idling_threshold_s)
        if dead_lettered:
            _log.info(
                "[delivery] dead-lettered %s stale claimed row(s)",
                dead_lettered,
            )
        stale_resurrects = dead_letter_stale_pending_resurrects(pool, threshold_s)
        if stale_resurrects:
            _log.info(
                "[delivery] dead-lettered %s stale pending resurrect row(s)",
                stale_resurrects,
            )
        stale_terminated = dead_letter_stale_pending_terminated(pool, threshold_s)
        if stale_terminated:
            _log.info(
                "[delivery] dead-lettered %s stale pending terminated-owner row(s)",
                stale_terminated,
            )
        stale_pending_chats = dead_letter_stale_pending_chats(pool, threshold_s)
        if stale_pending_chats:
            _log.info(
                "[delivery] dead-lettered %s stale pending chat row(s) of terminated owner(s)",
                stale_pending_chats,
            )
    except Exception:
        _log.exception("[delivery] stale-inbound dead-letter sweep failed")
    return now_mono


async def _scan_loop(pool: ConnectionPool, progress: LoopProgress) -> None:
    """Observe pending inputs and retire stale rows; never re-publish wakes."""
    interval = settings.daemon.delivery_watchdog_interval_seconds
    alerted: set[int] = set()
    last_sweep = 0.0
    while True:
        await round_loop.sleep_with_progress(progress, interval)
        if admission.quiesced():
            continue
        _, alerted = scan_once(pool, settings.daemon.delivery_watchdog_threshold_seconds, alerted)
        last_sweep = _maybe_sweep_stale_inbounds(
            pool,
            settings.daemon.delivery_watchdog_stale_claimed_threshold_seconds,
            settings.daemon.delivery_watchdog_stale_claimed_idling_threshold_seconds,
            last_sweep,
        )


async def run(*, database: Callable[[], Database], image: LoadedCommit) -> None:
    """Start the daemon: pidfile -> healthz server -> connect DB -> loops."""
    if _is_running():
        _log.info("[delivery] daemon already running (pidfile=%s), exiting", _pidfile())
        sys.exit(1)

    _write_pidfile()
    _log.info("[delivery] pidfile written: %s", _pidfile())

    liveness = LivenessGroup()
    endpoint = _endpoint()
    health = await start_health_server(
        "delivery_watchdog", endpoint.health_port, liveness=liveness, image=image
    )
    _log.info("[delivery] healthz listening on :%s", endpoint.health_port)

    # Four loops share the pool; each borrows a connection only for the length
    # of one short statement batch.
    db = database()
    pool = db.pool(max_size=_POOL_MAX_SIZE)
    try:
        scan = liveness.register("scan", _SCAN_LIVENESS_TIMEOUT_S)
        await _scan_loop(pool, scan)
    finally:
        pool.close()
        await stop_health_server(health)
        _remove_pidfile()
        _log.info("[delivery] daemon stopped")


def main() -> None:
    """Entry point: init logger + run asyncio loop."""
    from base.deploy.schema.migrations import assert_schema_current

    image = LoadedCommit.capture()
    machine_config = ConfigBoot()
    machine_config.boot()
    machine = machine_from_boot(machine_config)
    process_home = ava_home().resolve()
    # Pre-startup sanity: schema version must match code; raises SchemaVersionMismatch if not.
    assert_schema_current(settings.data_plane.db_url)
    version = CodeVersion(image)
    gate = ProcessDbGate(version=version.get, process="delivery_watchdog")

    def database() -> Database:
        return Database.from_settings(gate=gate, local_host=machine.host)

    pipeline = build_pipeline(database=database, machine=machine, home=process_home)
    init_gateway_process(
        name="delivery_watchdog",
        producer=lambda: pipeline,
        machine_reader=machine.name,
        image=image,
    )
    install_graceful_shutdown("delivery_watchdog")
    code = 0
    # `asyncio.Runner`, not `asyncio.run`: `run` closes in a `finally` that
    # awaits `shutdown_default_executor`, joining the default executor's
    # workers — and a stop signal must never wait on those (see `_hard_exit`).
    # The runner is therefore never closed: after the explicit drain below,
    # teardown is skipped by the hard exit.
    runner = asyncio.Runner()
    try:
        runner.run(run(database=database, image=image))
    except KeyboardInterrupt:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)  # a retry must not abort the bounded exit
        _log.info("[delivery] interrupted, shutting down")
        # The signal path skips Runner's own cancellation, so drain the loop's
        # tasks explicitly: `run`'s finally still stops the health server,
        # closes the DB pool and removes the pidfile. The executor is
        # deliberately NOT drained.
        failures = cancel_and_drain(runner)
        if failures:
            _log.error("[delivery] async shutdown failed: %r", failures)
            code = 1
    except Exception:
        _log.exception("[delivery] fatal error, shutting down")
        code = 1
    _hard_exit(code)


if __name__ == "__main__":
    from base.log import prepare_process_logging

    prepare_process_logging()
    main()
