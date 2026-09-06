"""Delivery watchdog daemon — gateway-owned wake dispatcher + recovery scanner.

Five jobs on one fast tick (user-confirmed design, 2026-08-02 — see
`delivery-dispatcher-design-2026-08-02.md`):

1. **Wake dispatch** — every `AVA_DELIVERY_WATCHDOG_INTERVAL_SECONDS` (default
   0.5s), re-publish the Redis wake for every `pending` inbound whose owner is
   `idling` and whose row is older than `AVA_DELIVERY_WATCHDOG_DISPATCH_THRESHOLD_SECONDS`
   (default 1s). Per-row dispatch counts apply a configurable backoff ladder;
   after the cap, the row is poisoned and no longer re-published by the
   watchdog. Poisoned rows remain pending and claimable. A publish that was
   lost (pub/sub is fire-and-forget) is thus retried without allowing a
   permanently failing inbound to create an unbounded wake storm. The
   per-agent 30s recheck stays as the double-fault safety net (dispatcher dead
   AND wake lost) and its degraded-WARNING doubles as a dispatcher-health
   signal.

2. **Stall alerting** — WARNING each chat inbound still `pending` past
   `AVA_DELIVERY_WATCHDOG_THRESHOLD_SECONDS` (default 30s) whose owner is in a
   waiting/terminal state (idling / terminated). Once-per-row
   while it stays pending: a memory set of already-alerted inbound ids is
   pruned each scan to the rows still pending, so a row that flips
   pending -> claimed -> pending (reconcile reset) alerts again. The set is
   persisted (Task #945): a daemon restart re-seeds it from
   `delivery_watchdog_alerted` instead of re-reporting every still-stalled
   inbound (the 5,184-event re-report burst, 2026-08-06 audit).

`running` owners are never dispatched or alerted: a chat queued behind a long
in-flight turn is normal — the claim's turn-end SELECT picks it up. Boot states
(restarting) is left to its own reaper.

3. **Terminated-owner resurrect retry** — every tick, for each DISTINCT
   terminated agent that still holds a `pending` chat created after its latest
   termination (the delivery-path auto-resurrect failed), re-run
   `resurrect_if_terminated`.
   This extends the delivery check from live owners to ALL agents (Task #689
   G4, user ruling 2026-08-03): a chat to a dead agent must wake it, and a
   missed auto-resurrect must be retried, not just alerted. Per-agent cooldown
   (60s) + per-tick cap + concurrency semaphore keep a pile of dead letters
   from spawning an LLM wake storm. Repeated failures suppress automatic wakes
   for a bounded, exponentially increasing per-agent window; new messages stay
   pending and normal delivery resumes after expiry.
4. **Stale-inbound dead-letter sweep** — every 30s, flip `claimed` chat
   inbounds of TERMINATED owners older than
   `AVA_DELIVERY_WATCHDOG_STALE_CLAIMED_THRESHOLD_SECONDS` (default 24h), or
   IDLING owners older than
   `AVA_DELIVERY_WATCHDOG_STALE_CLAIMED_IDLING_THRESHOLD_SECONDS` (default 2h),
   to `done` (age from `claimed_at`, falling back to `created_at`). Hosted
   idling agents may never boot again to reconcile their completed claims;
   running/restarting owners remain untouched. The same cadence also completes
   stale pending `terminate`, `system_note`, and `restart_completed` rows of
   terminated owners, which have no consumer. The reconcile-side cutoff
   (`agent/db.py::reconcile_claimed_inbounds`) still closes the terminated-owner
   resurrect race at boot.
5. **Hosted-turn liveness recovery** — on the same watchdog tick, select hosted
   running rows whose DB activity is older than the 2400s wedged-agent budget,
   then confirm them against the agent-host's 15s Redis progress heartbeat
   (60s TTL). Missing host heartbeats or stale per-turn marks trigger a
   terminate-then-resurrect recovery with a 10-minute per-agent cooldown.

Runs on the gateway, one per cluster. Kept alive via
`services/healthchecks/delivery_watchdog.py` (the gateway watchdog).

Usage:
    .venv/bin/python -m services.delivery_watchdog.daemon
"""

import asyncio
import logging
import os
import sys
import time

import psycopg
from psycopg import sql
from psycopg_pool import ConnectionPool

import shared.db
from services._pidfile import acquire_pidfile, pidfile_holds_daemon, remove_pidfile
from services.delivery_watchdog import dispatch_guard, resurrect_guard, turn_liveness
from shared import telemetry
from shared.agents import AgentStatus
from shared.config import settings
from shared.config.service_read import current_field_values
from shared.daemon_health import Liveness, health_port, start_health_server, stop_health_server
from shared.daemon_shutdown import install_graceful_shutdown
from shared.db_transaction import write_transaction
from shared.log import init_gateway_process
from shared.paths import legacy_pid_path

_log = logging.getLogger("services.delivery_watchdog.daemon")

_PIDFILE = settings.services.delivery_watchdog_pidfile
# Liveness staleness ceiling. The loop sleeps a short inter-poll interval
# (default 30s), so `_sleep_with_liveness` beats every _LIVENESS_BEAT_STEP_S
# during that wait; the ceiling only has to exceed that step, not the whole
# interval.
_LIVENESS_TIMEOUT_S = 60.0
_LIVENESS_BEAT_STEP_S = 15.0

# Terminated-owner resurrect retry (G4): a pending chat whose owner is
# terminated means the delivery-path auto-resurrect failed (or the delivery
# predates it). Retry it here, bounded against storms:
#   * per-agent cooldown — a failed resurrect (unreachable home machine) is
#     re-attempted at most once a minute, not every tick;
#   * concurrency semaphore — at most 2 resurrects in flight at once;
#   * per-tick cap (settings.delivery_watchdog_max_resurrect_per_tick) — a
#     pile of dead letters drains over ticks, never as a burst.
_RESURRECT_RETRY_MIN_INTERVAL_S = 60.0
_RESURRECT_MAX_CONCURRENCY = 2


def select_stale_pending(
    pool: ConnectionPool,
    threshold_s: float,
) -> list[tuple[int, int, str | None, float]]:
    """Chat inbounds still `pending` past `threshold_s`, as
    `(inbound_id, agent_id, agent_label, age_seconds)`, oldest first.

    An owner mid-turn (status='running') queues inbound legitimately — the
    claim's turn-end SELECT picks them up, so they are NOT stalls (a long LLM
    turn with a queued user message is normal). 'restarting' has its own reaper
    (boot_reap_grace_seconds); alerting at the 30s threshold would fire
    during a mass rollout. Only waiting/terminal owners signal a real stall:
    'idling' (lost wake), 'terminated' (delivery auto-resurrect failed).
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


def select_terminated_owners_with_pending(pool: ConnectionPool) -> list[tuple[int, int]]:
    """One `(agent_id, trigger_inbound_id)` per terminated owner with a
    post-termination pending chat, ordered by agent id.

    The selected chat is carried to the home runner as the final resurrection
    CAS. A chat already pending when the agent was terminated cannot reverse
    that explicit lifecycle decision, and a later termination makes this
    trigger stale before it can launch. Chat only: lifecycle kinds (terminate /
    restart) must not resurrect a dead agent against the caller's intent. A
    pile of 250 dead letters for one agent still means one attempt, not 250.
    """
    from shared.lifecycle_acceptance import FAILED_RESTART_FOR_CURRENT_TARGET
    from shared.resurrection_launch import PENDING_ALLOCATION

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                "SELECT m.agent_id, MIN(m.id) "
                "FROM inbound_messages m "
                "JOIN agents_meta ON agents_meta.id = m.agent_id "
                "WHERE m.status = 'pending' AND m.kind = 'chat' "
                "  AND ((agents_meta.status = 'terminated' AND NOT {} "
                " AND m.created_at > agents_meta.status_changed_at) OR {}) "
                "  AND m.id > COALESCE(agents_meta.last_force_terminate_inbound_id, 0) "
                "  AND (agents_meta.wake_suppressed_until IS NULL "
                "       OR agents_meta.wake_suppressed_until < now()) "
                "GROUP BY m.agent_id "
                "ORDER BY m.agent_id"
            ).format(sql.SQL(FAILED_RESTART_FOR_CURRENT_TARGET), sql.SQL(PENDING_ALLOCATION))
        )
        return [(r[0], r[1]) for r in cur.fetchall()]


def dead_letter_stale_claimed(
    pool: ConnectionPool,
    threshold_s: float,
    idling_threshold_s: float = 7200.0,
) -> int:
    """Dead-letter stale claimed chats of terminated or idling owners.

    Terminated agents leave 'claimed' rows behind: reconcile runs only at
    process boot, so a cleanly terminated (or long-dead) process never
    finalizes its claims. The rows sit forever — and worse, if the agent is
    ever resurrected (delivery auto-resurrect, Task #689 G4, or manually),
    boot reconcile sees no commit evidence (checkpoint pruned) and resets
    them all to 'pending', re-delivering ancient messages as fresh ones
    (Task #654). Dead-lettering rows older than the threshold keeps the
    two-phase crash-recovery guarantee (rows younger than the threshold still
    reset to 'pending' on boot) while making a resurrected agent start from
    its real conversation, not a flood of stale mail.

    Age is measured from `claimed_at`, falling back to `created_at` for rows
    that predate the claimed_at column (2026-08-02): a NULL claimed_at means
    'claimed before the column existed', so created_at is the only age
    evidence left. Running owners are never touched; idling owners are swept
    past the idling threshold because hosted agents may never boot again to
    finalize their claims.
    """
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE inbound_messages m SET status = 'done' "
            "FROM agents_meta am "
            "WHERE m.agent_id = am.id "
            "  AND m.status = 'claimed' AND m.kind = 'chat' "
            "  AND ((am.status = 'terminated' "
            "        AND COALESCE(m.claimed_at, m.created_at) "
            "            < now() - make_interval(secs => %s)) "
            "       OR (am.status = 'idling' "
            "           AND COALESCE(m.claimed_at, m.created_at) "
            "               < now() - make_interval(secs => %s)))",
            (threshold_s, idling_threshold_s),
        )
        return cur.rowcount


def dead_letter_stale_pending_resurrects(pool: ConnectionPool, threshold_s: float) -> int:
    """Dead-letter pending resurrect rows whose consumer never reached claim.

    A stale lifecycle row records an abandoned wake. Retaining it cannot
    recover the turn and later floods the agent with redundant markers, so age
    alone decides cleanup regardless of the current agent lifecycle state.
    """
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE inbound_messages m SET status = 'done', claimed_at = now() "
            "WHERE m.status = 'pending' AND m.kind = 'resurrect' "
            "  AND m.created_at < now() - make_interval(secs => %s)",
            (threshold_s,),
        )
        return cur.rowcount


def dead_letter_stale_pending_terminated(pool: ConnectionPool, threshold_s: float) -> int:
    """Complete stale lifecycle notices whose terminated owner cannot claim them.

    Post-termination chats remain pending for the G4 resurrect-retry path; only
    one-shot lifecycle notices with no remaining consumer are dead-lettered.
    """
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE inbound_messages m SET status = 'done', claimed_at = now() "
            "FROM agents_meta am "
            "WHERE m.agent_id = am.id "
            "  AND am.status = 'terminated' "
            "  AND m.status = 'pending' "
            "  AND m.kind IN ('terminate', 'system_note', 'restart_completed') "
            "  AND m.created_at < now() - make_interval(secs => %s)",
            (threshold_s,),
        )
        return cur.rowcount


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
    """One scan: WARNING each chat inbound stalled past `threshold_s` that is
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
    """One WARNING per stalled row: logger line + unified event (feeds the
    frontend SSE stream / metrics via the events table). emit failure is
    logged, never raised — the WARNING log line is the primary alert."""
    _log.warning(
        "[delivery] inbound %s to agent %s (%s) still pending after %.0fs — "
        "delivery stalled; pub/sub wake lost or claim not running",
        inbound_id,
        agent_id,
        label or f"#{agent_id}",
        age_s,
    )
    # ts = process clock at enqueue (emitter stamps datetime.now(UTC)) — the
    # old direct agent_events INSERT used DB now(); see services/heartbeat/
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
    if not acquire_pidfile(_PIDFILE, "services.delivery_watchdog.daemon"):
        _log.info("[delivery_watchdog] daemon already running (pidfile=%s), exiting", _PIDFILE)
        sys.exit(1)


def _remove_pidfile() -> None:
    remove_pidfile(_PIDFILE)


def _is_running() -> bool:
    """Whether a daemon is already running (via pidfile, new + legacy paths).

    Pid-reuse-safe: a live pid whose argv does not name this daemon's module
    is a recycled pid, not a running instance (audit round 2, P1)."""
    return pidfile_holds_daemon(
        _PIDFILE, "services.delivery_watchdog.daemon"
    ) or pidfile_holds_daemon(
        legacy_pid_path("delivery_watchdog"), "services.delivery_watchdog.daemon"
    )


async def _sleep_with_liveness(liveness: Liveness, total_s: float) -> None:
    """Sleep `total_s`, beating liveness every `_LIVENESS_BEAT_STEP_S` so the
    /healthz probe stays fresh instead of reading as a wedged loop."""
    remaining = total_s
    while remaining > 0:
        liveness.beat()
        step = min(_LIVENESS_BEAT_STEP_S, remaining)
        await asyncio.sleep(step)
        remaining -= step


# ── Terminated-owner resurrect retry (G4) ────────────────────────────────────
_last_resurrect_attempt: dict[int, float] = {}
_resurrect_tasks: dict[int, asyncio.Task[None]] = {}
_resurrect_failures: dict[int, int] = {}
_resurrect_suppressions: dict[int, int] = {}
_resurrect_semaphore = asyncio.Semaphore(_RESURRECT_MAX_CONCURRENCY)


async def _resurrect_one(pool: ConnectionPool, agent_id: int, trigger_inbound_id: int) -> None:
    """Run `resurrect_if_terminated` for one agent, bounded by the concurrency
    semaphore; classify the returned status and escalate consecutive failures
    into a durable wake-suppression window."""
    from ops.ops_lifecycle import resurrect_if_terminated

    async with _resurrect_semaphore:
        try:
            status = await resurrect_if_terminated(
                agent_id,
                trigger_inbound_id=trigger_inbound_id,
                trigger_inbound_kind="chat",
            )
            if status is AgentStatus.TERMINATED:
                resurrect_guard.record_resurrect_failure(
                    pool,
                    agent_id,
                    _resurrect_failures,
                    _resurrect_suppressions,
                )
            else:
                _resurrect_failures.pop(agent_id, None)
                _resurrect_suppressions.pop(agent_id, None)
                _log.info(
                    "[delivery] resurrect retry for terminated agent %s -> status %s",
                    agent_id,
                    status,
                )
        except Exception:
            _log.info(
                "[delivery] resurrect retry failed for agent %s",
                agent_id,
                exc_info=True,
            )
            resurrect_guard.record_resurrect_failure(
                pool,
                agent_id,
                _resurrect_failures,
                _resurrect_suppressions,
            )
        finally:
            _last_resurrect_attempt[agent_id] = time.monotonic()


def _maybe_spawn_resurrects(pool: ConnectionPool, max_per_tick: int) -> None:
    """Enqueue a resurrect retry per distinct terminated owner with a pending
    chat, honoring the per-agent cooldown and the per-tick cap. Fire-and-forget
    (the tick is not blocked on a resurrect's RPC timeouts); tasks are tracked
    so they are never garbage-collected mid-flight.

    The cap bounds how many new spawns THIS tick attempts (audit round 2,
    P2/P3): it used to compare the cross-tick in-flight task set against the
    per-tick quota, so two slow in-flight resurrects starved every later tick,
    and the backlog warning re-ran the owners query once per overflow."""
    now = time.monotonic()
    owners = select_terminated_owners_with_pending(pool)
    spawned = 0
    deferred = 0
    for agent_id, trigger_inbound_id in owners:
        if agent_id in _resurrect_tasks:
            continue
        if now - _last_resurrect_attempt.get(agent_id, 0.0) < _RESURRECT_RETRY_MIN_INTERVAL_S:
            continue
        if spawned >= max_per_tick:
            deferred += 1
            continue
        task = asyncio.create_task(_resurrect_one(pool, agent_id, trigger_inbound_id))
        _resurrect_tasks[agent_id] = task

        def _discard_completed_task(
            completed: asyncio.Task[None], *, completed_agent_id: int = agent_id
        ) -> None:
            if _resurrect_tasks.get(completed_agent_id) is completed:
                del _resurrect_tasks[completed_agent_id]

        task.add_done_callback(_discard_completed_task)
        spawned += 1
    if deferred:
        _log.warning(
            "[delivery] resurrect retry backlog: %s more terminated owner(s) deferred",
            deferred,
        )
    if spawned:
        _log.info("[delivery] spawned %s resurrect retry task(s)", spawned)


# Alert-dedup GC cadence (Task #945): the TTL sweep runs once per
# `_DEDUP_GC_EVERY_TICKS` ticks (120 ticks x 0.5s default interval = 1/min);
# the per-tick prune is the primary GC, this is the safety net.
_DEDUP_TTL_S = 7 * 24 * 3600.0
_DEDUP_GC_EVERY_TICKS = 120

# Stale-claimed dead-letter cadence (Task #654): the sweep is time-gated (not
# tick-gated) so its real period is independent of the tick interval. 30s is
# plenty — the hazard is a resurrect re-delivering ancient rows, and a
# resurrect takes seconds to boot, so the sweep is virtually always ahead of
# it; the reconcile-side cutoff (agent/db.py) closes the residual race.
_CLAIMED_SWEEP_INTERVAL_S = 30.0


def _hosted_turn_threshold_seconds() -> float:
    """Read the runner-owned threshold from the gateway's current `.env` view."""
    return float(current_field_values()["wedged_agent_inbound_age_seconds"])


def _maybe_sweep_stale_claimed(
    pool: ConnectionPool,
    threshold_s: float,
    idling_threshold_s: float,
    last_sweep_at: float,
) -> float:
    """Run stale-inbound dead-letter sweeps on the configured cadence.

    Return the new monotonic sweep timestamp. Both sweeps are best-effort: a
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
    except Exception:
        _log.exception("[delivery] stale-inbound dead-letter sweep failed")
    return now_mono


async def _scan_loop(pool: ConnectionPool, liveness: Liveness) -> None:
    """Main loop: every interval, (1) re-publish lost wakes for stale pending
    rows of idling owners, (2) WARNING each chat inbound stalled past the alert
    threshold, once per row while it stays pending, (3) retry resurrect for
    terminated owners with pending chats.

    The once-per-row alert set lives in `delivery_watchdog_alerted` — the
    table is the single truth (Task #945); each tick reloads it, so memory
    holds only a per-tick working copy and a daemon restart never re-reports
    every still-stalled inbound (R1 old-signal sweep, PR5)."""
    interval = settings.daemon.delivery_watchdog_interval_seconds
    dispatch_threshold = settings.daemon.delivery_watchdog_dispatch_threshold_seconds
    max_dispatch_count = settings.daemon.delivery_watchdog_max_dispatch_count
    dispatch_backoff_steps = settings.daemon.delivery_watchdog_dispatch_backoff_steps_s
    alert_threshold = settings.daemon.delivery_watchdog_threshold_seconds
    stale_claimed_threshold = settings.daemon.delivery_watchdog_stale_claimed_threshold_seconds
    stale_claimed_idling_threshold = (
        settings.daemon.delivery_watchdog_stale_claimed_idling_threshold_seconds
    )
    # This alias belongs to the agent-runner config projection and is removed
    # from the gateway process environment. Read the gateway-owned `.env`
    # snapshot so an operator override is preserved at this authority boundary.
    hosted_turn_threshold = _hosted_turn_threshold_seconds()
    _log.info(
        "[delivery] watchdog started, pid=%s, interval=%.1fs, dispatch_threshold=%.1fs, "
        "alert_threshold=%.0fs, stale_claimed_threshold=%.0fs, "
        "stale_claimed_idling_threshold=%.0fs, "
        "hosted_turn_threshold=%.0fs, hosted_turn_cooldown=%.0fs, "
        "alert set table-backed (reload per tick)",
        os.getpid(),
        interval,
        dispatch_threshold,
        alert_threshold,
        stale_claimed_threshold,
        stale_claimed_idling_threshold,
        hosted_turn_threshold,
        turn_liveness.HOSTED_TURN_RECOVERY_COOLDOWN_S,
    )
    ticks = 0
    last_claimed_sweep = 0.0
    while True:
        try:
            await _sleep_with_liveness(liveness, interval)
            # Reload the alerted set from the table — it is the single truth;
            # `alerted` below is a per-tick working copy. An unreadable table
            # skips the whole tick (defer rather than re-alert): the loop
            # retries in `interval` seconds.
            try:
                alerted = select_alerted_ids(pool)
            except Exception:
                _log.exception("[delivery] could not reload alerted set — skipping tick")
                continue
            # scan_once prunes `alerted` IN PLACE (`alerted &= pending`), so
            # snapshot before the call — the delta below must compare against
            # the pre-scan set, not the mutated one.
            prev_alerted = set(alerted)
            dispatched = dispatch_wakes(
                pool,
                dispatch_threshold,
                max_dispatch_count,
                dispatch_backoff_steps,
            )
            newly_alerted, alerted = scan_once(pool, alert_threshold, alerted)
            # Persist the delta: new alerts INSERT, resolved rows DELETE, TTL
            # sweep on a slow cadence. A DB failure here is degraded, not
            # fatal — the next tick reloads from the table, so a failed
            # persist can at most cause one duplicate WARNING for the rows
            # that were newly alerted since the last successful write.
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
            _maybe_spawn_resurrects(pool, settings.daemon.delivery_watchdog_max_resurrect_per_tick)
            await turn_liveness.scan_hosted_turn_liveness(pool, hosted_turn_threshold)
            last_claimed_sweep = _maybe_sweep_stale_claimed(
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


async def run() -> None:
    """Start the daemon: pidfile -> healthz server -> connect DB -> main loop."""
    if _is_running():
        _log.info("[delivery] daemon already running (pidfile=%s), exiting", _PIDFILE)
        sys.exit(1)

    _write_pidfile()
    _log.info("[delivery] pidfile written: %s", _PIDFILE)

    liveness = Liveness(_LIVENESS_TIMEOUT_S)
    health = await start_health_server("delivery_watchdog", liveness=liveness)
    _log.info("[delivery] healthz listening on :%s", health_port("delivery_watchdog"))

    pool = shared.db.pool()
    try:
        await _scan_loop(pool, liveness)
    finally:
        pool.close()
        await stop_health_server(health)
        _remove_pidfile()
        _log.info("[delivery] daemon stopped")


def main() -> None:
    """Entry point: init logger + run asyncio loop."""
    from shared.migrations import assert_schema_current

    # Pre-startup sanity: schema version must match code; raises SchemaVersionMismatch if not.
    assert_schema_current(settings.data_plane.db_url)
    init_gateway_process(name="delivery_watchdog")
    install_graceful_shutdown("delivery_watchdog")
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        _log.info("[delivery] interrupted, shutting down")
    except Exception:
        _log.exception("[delivery] fatal error, shutting down")


if __name__ == "__main__":
    main()
