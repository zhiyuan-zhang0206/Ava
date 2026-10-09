"""Delivery watchdog daemon — gateway-owned wake dispatcher + recovery scanner.

Six jobs on four resident loops under one `TaskGroup` (`_run_loops`): jobs 1, 2
and 4 share the fast scan loop (user-confirmed design, 2026-08-02 — see
`delivery-dispatcher-design-2026-08-02.md`); jobs 3, 5 and 6 each run as their own
sequential loop, so an agent is never in two attempts at once and a slow RPC
holds up only its own loop. Their per-agent cooldowns and failure counts live in
`delivery_watchdog_attempts`, so a restart resumes them. A loop that raises ends
the process and the supervisor restarts it.

1. **Wake dispatch** — every `AVA_DELIVERY_WATCHDOG_INTERVAL_SECONDS` (default
   0.5s), re-publish the Redis wake for every `pending` inbound whose owner is
   `idling` and whose row is older than `AVA_DELIVERY_WATCHDOG_DISPATCH_THRESHOLD_SECONDS`
   (default 1s). Per-row dispatch counts apply a configurable backoff ladder;
   after the cap, the row is poisoned and no longer re-published by the
   watchdog; poisoned rows stay pending and claimable. A publish that was lost
   (pub/sub is fire-and-forget) is retried without letting a permanently
   failing inbound create an unbounded wake storm; the per-agent 30s recheck
   stays the double-fault safety net, its degraded-WARNING a dispatcher-health signal.
   Re-dispatch and poisoning apply only while the owner's host verdict is fresh
   (`machine_probe` younger than `AVA_DELIVERY_WATCHDOG_HOST_STALENESS_SECONDS`,
   machine graded online by the two-consecutive-failure rule, agent host
   alive — the host check is excused inside the one-failure grace window,
   when the failed probe itself nulls the host verdict); a missing, stale,
   graded-offline or (outside that window) host-less verdict freezes the row
   (no counter burn, no poison) and it resumes once the verdict is fresh
   again (task #4872 route D).

2. **Stall alerting** — alert each chat inbound still `pending` past
   `AVA_DELIVERY_WATCHDOG_THRESHOLD_SECONDS` (default 30s) whose owner is in a
   waiting/terminal state (idling / terminated), once per row while it stays
   pending: the alert is the unified `delivery_stalled` event (an INFO log line
   is the local trace), the alerted-id set is pruned to pending rows each scan
   (a row that flips pending -> claimed -> pending alerts again) and persisted
   (Task #945), so a daemon restart re-seeds from `delivery_watchdog_alerted`
   instead of re-reporting every still-stalled inbound (the 5,184-event burst,
   2026-08-06 audit). The event is the whole signal: Grafana Alerting decides
   whether and when it notifies.

`running` owners are never dispatched or alerted: a chat queued behind a long
in-flight turn is normal — the claim's turn-end SELECT picks it up.

3. **Terminated-owner resurrect retry** (`resurrect_retry`) — every round, for each DISTINCT
   terminated agent that still holds a `pending` chat created after its latest
   termination (the delivery-path auto-resurrect failed) and younger than the
   stale-claimed threshold, re-run `resurrect_if_terminated`. Older pending
   chats are dead letters: the reaper closes them and they never resurrect
   their owner again (issue #2049).
   This extends the delivery check from live owners to ALL agents (Task #689
   G4, user ruling 2026-08-03): a chat to a dead agent must wake it, and a
   missed auto-resurrect must be retried, not just alerted. Per-agent cooldown
   (60s, persisted) + per-round cap + concurrency semaphore keep a pile of dead letters
   from spawning an LLM wake storm; repeated failures suppress automatic wakes
   for a bounded exponentially increasing window, and normal delivery resumes after expiry.
4. **Stale-inbound dead-letter sweep** — every 30s, flip `claimed` chat
   inbounds of TERMINATED owners older than
   `AVA_DELIVERY_WATCHDOG_STALE_CLAIMED_THRESHOLD_SECONDS` (default 24h), or
   IDLING owners older than
   `AVA_DELIVERY_WATCHDOG_STALE_CLAIMED_IDLING_THRESHOLD_SECONDS` (default 2h),
   to `done` (age from `claimed_at`, falling back to `created_at`). Hosted
   idling agents may never boot again to reconcile their completed claims;
   running owners remain untouched. The same cadence completes the
   stale pending `terminate` / `system_note` / `restart_completed` rows of
   terminated owners (no consumer), and the reconcile-side cutoff
   (`agent/db/__init__.py::reconcile_claimed_inbounds`) still closes the resurrect race at boot.
5. **Hosted-turn liveness recovery** (`turn_liveness`) — every round, select hosted
   running rows whose DB activity is older than the 2400s wedged-agent budget,
   then confirm them against the agent-host's 15s Redis progress heartbeat
   (60s TTL). Missing host heartbeats or stale per-turn marks trigger a
   terminate-then-resurrect recovery with a persisted 10-minute per-agent cooldown; the
   recovery wake commits in the same transaction as the force terminate.

6. **Stalled crash-marked harvest request** (`stall_recovery`) — escalate a chat still `pending`
   past the stall threshold whose owner is a crash-marked idling corpse over
   the internal `recover-crash-marked-v2` path (one request per owner, persisted 60s
   cooldown, gated by `AVA_DELIVERY_STALLED_RECOVERY_ENABLED`; Task #3618).

Runs on the gateway, one per cluster. Kept alive by the root supervisor's health
monitor through the roster's `/healthz` identity probe (`ops/roster/healthz.py`).

Usage:
    .venv/bin/python -m services.wake.delivery_watchdog.daemon
"""

import asyncio
import logging
import os
import signal
import sys
import time
from pathlib import Path

import psycopg
from psycopg_pool import ConnectionPool

from base import paths, telemetry
from base.config import Settings, settings
from base.config.service_read import ConfigAuthority
from base.daemon import round_loop
from base.daemon.endpoints import ServiceEndpoint, ServiceEndpoints
from base.daemon.health import start_health_server, stop_health_server
from base.daemon.loop_health import LivenessGroup, LoopProgress
from base.daemon.shutdown import cancel_and_drain, install_graceful_shutdown
from base.daemon.shutdown import hard_exit as _hard_exit
from base.db import Database
from base.db.transaction import write_transaction
from base.deploy.maintenance import admission
from base.events.live.bus import EventBus
from base.log import init_gateway_process
from services.pidfile import acquire_pidfile, pidfile_holds_daemon, remove_pidfile
from services.wake.delivery_watchdog import (
    dispatch_guard,
    resurrect_retry,
    rounds,
    stall_recovery,
    turn_liveness,
)
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
# Connections the four loops' concurrent statements can hold at once.
_POOL_MAX_SIZE = 4


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


select_pending_for_dispatch = dispatch_guard.select_pending_for_dispatch
dispatch_wakes = dispatch_guard.dispatch_wakes


select_terminated_owners_with_pending = resurrect_retry.select_terminated_owners_with_pending


def select_pending_ids(pool: ConnectionPool) -> set[int]:
    """Every currently-pending inbound id — used to prune the alert set so a
    row that left `pending` stops being remembered (and re-alerts if it ever
    comes back)."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM inbound_messages WHERE status = 'pending'")
        return {r[0] for r in cur.fetchall()}


# ── Alert-dedup persistence (Task #945) ──────────────────────────────────────
#
# The once-per-row `alerted` set lives in memory; a daemon restart emptied it,
# so every inbound still stalled at boot re-alerted in one burst (5,184
# delivery_stalled events, 2026-08-06 audit). `delivery_watchdog_alerted`
# carries the set across restarts: seed at boot, INSERT on first alert, DELETE
# when the inbound leaves `pending` (keeps the table equal to the live set),
# TTL GC as a safety net. FK -> inbound_messages ON DELETE CASCADE covers
# inbound purges outside the prune path.


def select_alerted_ids(pool: ConnectionPool) -> set[int]:
    """The persisted already-alerted inbound ids — seeds the in-memory set at
    boot so a restart does not re-report every still-stalled inbound."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT inbound_id FROM delivery_watchdog_alerted")
        return {r[0] for r in cur.fetchall()}


def persist_alerted(pool: ConnectionPool, inbound_ids: set[int]) -> None:
    """Persist newly-alerted inbound ids, one row per id. Caller passes only
    the delta (ids not already alerted) so the write is proportional to new
    alerts, not to the set. ON CONFLICT DO NOTHING guards an id that was
    pruned and re-alerted between two scans."""
    if not inbound_ids:
        return
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO delivery_watchdog_alerted (inbound_id) VALUES (%s) "
            "ON CONFLICT (inbound_id) DO NOTHING",
            [(i,) for i in inbound_ids],
        )


def prune_alerted(pool: ConnectionPool, inbound_ids: set[int]) -> None:
    """Forget dedup rows for inbounds that left `pending`. Keeps the table
    equal to the in-memory set, so the pending -> claimed -> pending
    (reconcile reset) flip re-alerts exactly as it did with memory alone."""
    if not inbound_ids:
        return
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM delivery_watchdog_alerted WHERE inbound_id = ANY(%s)",
            (list(inbound_ids),),
        )


def gc_alerted(pool: ConnectionPool, ttl_s: float) -> int:
    """TTL safety net: drop dedup rows older than `ttl_s` that the per-scan
    prune never saw (the inbound left `pending` while the daemon was down, or
    an alert predates the table). Returns the number of rows removed."""
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM delivery_watchdog_alerted "
            "WHERE alerted_at < now() - make_interval(secs => %s)",
            (ttl_s,),
        )
        return cur.rowcount


def scan_once(
    pool: ConnectionPool,
    threshold_s: float,
    alerted: set[int],
) -> tuple[int, set[int]]:
    """One scan: alert each chat inbound stalled past `threshold_s` that is
    not already in `alerted`; prune `alerted` down to rows still pending.

    `alerted` is a per-tick working copy seeded from the table (the single
    truth, R1 old-signal sweep PR5). Returns
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


# Alert-dedup GC cadence (Task #945): the TTL sweep runs once per
# `_DEDUP_GC_EVERY_TICKS` ticks (120 ticks x 0.5s default interval = 1/min);
# the per-tick prune is the primary GC, this is the safety net.
_DEDUP_TTL_S = 7 * 24 * 3600.0
_DEDUP_GC_EVERY_TICKS = 120

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


def _stall_alert_tick(pool: ConnectionPool, threshold_s: float, ticks: int) -> tuple[int, int]:
    """One stall-alert half of a scan tick: reload the persisted alerted set,
    alert the rows it does not cover, persist the delta (INSERT new, DELETE
    resolved, TTL GC on the slow cadence). Returns `(newly_alerted, ticks)` —
    `ticks` advances only on a successful persist, so the GC cadence survives
    a database hiccup.

    A dedup table that cannot be read defers the alert half (never alert
    against a set that may already cover the rows); the wake re-dispatch and
    the dead-letter sweeps do not read the table and keep the tick's other
    half live."""
    try:
        alerted = select_alerted_ids(pool)
    except Exception:
        _log.exception("[delivery] could not reload alerted set — deferring stall alerts")
        return 0, ticks
    # scan_once prunes `alerted` IN PLACE (`alerted &= pending`), so snapshot
    # before the call — the delta below must compare against the pre-scan set,
    # not the mutated one.
    prev_alerted = set(alerted)
    newly_alerted, alerted = scan_once(pool, threshold_s, alerted)
    # Persist the delta: new alerts INSERT, resolved rows DELETE, TTL sweep on
    # a slow cadence. A DB failure here is degraded, not fatal — the next tick
    # reloads from the table, so a failed persist can at most cause one
    # duplicate alert for the rows alerted since the last successful write.
    try:
        new_ids = alerted - prev_alerted
        if new_ids:
            persist_alerted(pool, new_ids)
        removed = prev_alerted - alerted
        if removed:
            prune_alerted(pool, removed)
        ticks += 1
        if ticks % _DEDUP_GC_EVERY_TICKS == 0:
            gc_alerted(pool, _DEDUP_TTL_S)
    except Exception:
        _log.exception("[delivery] alert-dedup persist/prune failed")
    return newly_alerted, ticks


async def _scan_loop(
    pool: ConnectionPool, db: Database, bus: EventBus, progress: LoopProgress
) -> None:
    """Scan loop: every interval, (1) re-publish lost wakes for stale pending
    rows of idling owners, (2) alert each chat inbound stalled past the alert
    threshold, once per row while it stays pending, (3) sweep stale inbounds
    into dead letters. The three RPC-driven recovery jobs run as their own
    loops beside this one (`run`).

    The once-per-row alert set lives in `delivery_watchdog_alerted` — the
    table is the single truth (Task #945); each tick reloads it, so memory
    holds only a per-tick working copy and a daemon restart never re-reports
    every still-stalled inbound (R1 old-signal sweep, PR5)."""
    interval = settings.daemon.delivery_watchdog_interval_seconds
    dispatch_threshold = settings.daemon.delivery_watchdog_dispatch_threshold_seconds
    max_dispatch_count = settings.daemon.delivery_watchdog_max_dispatch_count
    dispatch_backoff_steps = settings.daemon.delivery_watchdog_dispatch_backoff_steps_s
    host_staleness = settings.daemon.delivery_watchdog_host_staleness_seconds
    alert_threshold = settings.daemon.delivery_watchdog_threshold_seconds
    stale_claimed_threshold = settings.daemon.delivery_watchdog_stale_claimed_threshold_seconds
    stale_claimed_idling_threshold = (
        settings.daemon.delivery_watchdog_stale_claimed_idling_threshold_seconds
    )
    _log.info(
        "[delivery] watchdog started, pid=%s, interval=%.1fs, dispatch_threshold=%.1fs, "
        "host_staleness=%.0fs, alert_threshold=%.0fs, "
        "stale_claimed_threshold=%.0fs, stale_claimed_idling_threshold=%.0fs, "
        "alert set table-backed (reload per tick)",
        os.getpid(),
        interval,
        dispatch_threshold,
        host_staleness,
        alert_threshold,
        stale_claimed_threshold,
        stale_claimed_idling_threshold,
    )
    ticks = 0
    last_claimed_sweep = 0.0
    while True:
        try:
            await round_loop.sleep_with_progress(progress, interval)
            if admission.quiesced():
                continue
            dispatched = dispatch_wakes(
                pool,
                db,
                bus,
                dispatch_threshold,
                max_dispatch_count,
                dispatch_backoff_steps,
                host_staleness,
            )
            newly_alerted, ticks = _stall_alert_tick(pool, alert_threshold, ticks)
            last_claimed_sweep = _maybe_sweep_stale_inbounds(
                pool,
                stale_claimed_threshold,
                stale_claimed_idling_threshold,
                last_claimed_sweep,
            )
            if dispatched or newly_alerted:
                _log.info(
                    "[delivery] tick: %d wake(s) re-dispatched, %d newly alerted",
                    dispatched,
                    newly_alerted,
                )
        except asyncio.CancelledError:
            raise
        except psycopg.ProgrammingError:
            _log.critical(
                "[delivery] schema / syntax error — code<->DB drift; retry will not self-heal, daemon exiting, restart after fix",
                exc_info=True,
            )
            raise
        except Exception:
            _log.exception("[delivery] poll iteration failed")


async def _run_loops(
    pool: ConnectionPool,
    db: Database,
    bus: EventBus,
    liveness: LivenessGroup,
    *,
    authority: ConfigAuthority,
) -> None:
    """Own the four resident loops: the scan loop and the three recovery loops
    (resurrect retry, stalled crash-marked harvest, hosted-turn recovery).

    One `TaskGroup` holds them, so a loop that raises cancels its siblings and
    the exception leaves `run`: the process exits and the supervisor restarts
    it. Each loop reports its own progress, so a wedged loop reads as a
    failing `/healthz` even while its siblings stay busy."""
    interval = settings.daemon.delivery_watchdog_interval_seconds
    scan = liveness.register("scan", _SCAN_LIVENESS_TIMEOUT_S)
    resurrect = liveness.register("resurrect", rounds.loop_liveness_timeout_s())
    harvest = liveness.register("harvest", rounds.loop_liveness_timeout_s())
    hosted_turn = liveness.register("hosted_turn", rounds.loop_liveness_timeout_s())
    async with asyncio.TaskGroup() as loops:
        loops.create_task(_scan_loop(pool, db, bus, scan))
        loops.create_task(
            resurrect_retry.resurrect_loop(
                pool,
                db,
                bus,
                resurrect,
                interval,
                settings.daemon.delivery_watchdog_max_resurrect_per_tick,
                settings.daemon.delivery_watchdog_stale_claimed_threshold_seconds,
            )
        )
        loops.create_task(
            stall_recovery.stall_recovery_loop(
                pool,
                db,
                bus,
                harvest,
                interval,
                settings.daemon.delivery_watchdog_threshold_seconds,
            )
        )
        loops.create_task(
            turn_liveness.hosted_turn_recovery_loop(
                pool,
                db,
                bus,
                hosted_turn,
                interval,
                turn_liveness.hosted_turn_threshold_seconds(authority),
            )
        )


async def run() -> None:
    """Start the daemon: pidfile -> healthz server -> connect DB -> loops."""
    if _is_running():
        _log.info("[delivery] daemon already running (pidfile=%s), exiting", _pidfile())
        sys.exit(1)

    _write_pidfile()
    _log.info("[delivery] pidfile written: %s", _pidfile())

    liveness = LivenessGroup()
    endpoint = _endpoint()
    health = await start_health_server("delivery_watchdog", endpoint.health_port, liveness=liveness)
    _log.info("[delivery] healthz listening on :%s", endpoint.health_port)

    # Four loops share the pool; each borrows a connection only for the length
    # of one short statement batch.
    db = Database.from_settings()
    pool = db.pool(max_size=_POOL_MAX_SIZE)
    try:
        authority = ConfigAuthority(
            runtime=settings,
            all_domains=settings if settings.profile is None else Settings(profile=None),
            env_path=paths.ava_home() / ".env",
        )
        await _run_loops(pool, db, EventBus.from_settings(), liveness, authority=authority)
    finally:
        pool.close()
        await stop_health_server(health)
        _remove_pidfile()
        _log.info("[delivery] daemon stopped")


def main() -> None:
    """Entry point: init logger + run asyncio loop."""
    from base.deploy.schema.migrations import assert_schema_current

    # Pre-startup sanity: schema version must match code; raises SchemaVersionMismatch if not.
    assert_schema_current(settings.data_plane.db_url)
    init_gateway_process(name="delivery_watchdog")
    install_graceful_shutdown("delivery_watchdog")
    code = 0
    # `asyncio.Runner`, not `asyncio.run`: `run` closes in a `finally` that
    # awaits `shutdown_default_executor`, joining the default executor's
    # workers — and a stop signal must never wait on those (see `_hard_exit`).
    # The runner is therefore never closed: after the explicit drain below,
    # teardown is skipped by the hard exit.
    runner = asyncio.Runner()
    try:
        runner.run(run())
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
    main()
