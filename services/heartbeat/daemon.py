"""Heartbeat daemon — gateway-owned idle-agent check-in dispatcher.

It selects idle agents that have been parked past `AVA_HEARTBEAT_IDLE_THRESHOLD_SECONDS`
(default 5 min) and have not paused their heartbeat, and INSERTs a `heartbeat`
check-in inbound to each. The inbound-insert trigger wakes the agent (on any
machine — this is cluster-wide, not machine-scoped: unlike the agent host it never
touches local sessions). Runs on the gateway, one per cluster.

Dispatch is paced for fleet scale: rather than checking in on every due agent in
one batch per idle window (which would wake 100-300 idle agents simultaneously —
a decompression + LLM thundering herd, self-perpetuating because the check-in
resets each agent's idle clock in lockstep), it polls on a fine cadence (min of
`AVA_HEARTBEAT_INTERVAL_SECONDS` and `_DISPATCH_STEP_S`), de-phases due-times with a
deterministic per-agent jitter, and caps check-ins per step for a hard global
wake-rate ceiling. See the "Wakeup-storm flattening" note below.

Usage:
    .venv/bin/python -m services.heartbeat.daemon

Kept alive via `services/healthchecks/heartbeat.py` (the gateway watchdog).
"""

import asyncio
import contextlib
import logging
import math
import os
import sys
import time

import psycopg
from psycopg_pool import ConnectionPool

import shared.db
from services._pidfile import acquire_pidfile, pidfile_holds_daemon, remove_pidfile
from services.heartbeat import JITTER_SPAN_S, STALE_PENDING_S
from services.heartbeat.liveness import _PASS_INTERVAL_S, run_liveness_pass
from shared import telemetry
from shared.config import settings
from shared.daemon_health import Liveness, health_port, start_health_server, stop_health_server
from shared.daemon_shutdown import install_graceful_shutdown
from shared.db_transaction import write_transaction
from shared.log import init_gateway_process
from shared.paths import legacy_pid_path

_log = logging.getLogger("services.heartbeat.daemon")

_PIDFILE = settings.services.heartbeat_pidfile
# Liveness staleness ceiling. The loop sleeps a long inter-poll interval (default
# 300s), so `_sleep_with_liveness` beats every _LIVENESS_BEAT_STEP_S during that
# wait; the ceiling only has to exceed that step, not the whole interval. A
# genuinely wedged loop (a beat step that never returns) still trips /healthz 503
# -> watchdog respawn.
_LIVENESS_TIMEOUT_S = 60.0
_LIVENESS_BEAT_STEP_S = 30.0

# ── Wakeup-storm flattening (density hardening) ──
# A check-in wakes an idle agent: its ~90MB compressed heap decompresses (~25-40ms
# of CPU on Apple Silicon) and it opens an LLM turn. Firing every due agent in one
# tight loop therefore triggers a simultaneous decompression + LLM burst; worse,
# the woken agent runs a turn, which resets its `last_active_at` (the idle clock),
# so a batch woken together comes due together next cycle — the fleet
# self-synchronizes into a
# recurring spike. Three coupled defenses keep the wake rate bounded at fleet
# scale (100-300 idle agents):
#   * poll on a fine cadence (_DISPATCH_STEP_S), not once per idle window, so due
#     agents are noticed in small time-slices instead of one 300s batch;
#   * de-phase each agent's due-time by a deterministic per-agent jitter
#     (JITTER_SPAN_S) so a fleet that went idle together does not come due
#     together — this breaks the self-synchronization;
#   * cap check-ins per dispatch step (_MAX_CHECKINS_PER_STEP) for a hard global
#     wake-rate ceiling (~_MAX_CHECKINS_PER_STEP / _DISPATCH_STEP_S per second),
#     draining even a fully-synchronized backlog (e.g. after a mass restart)
#     smoothly instead of in one instantaneous spike.
# Steady-state at 300 idle agents (idle_threshold 300s) the natural due-rate is
# ~1/s, under the ~1.7/s ceiling, so jitter alone carries it; the cap only bites
# on a synchronized burst. Trade-off: a fully-synchronized backlog's tail agents
# wait longer for their first check-in (backlog / ceiling seconds) — acceptable
# for a liveness check-in that is a safety net, not a latency-sensitive signal.
_DISPATCH_STEP_S = 15.0
# JITTER_SPAN_S / STALE_PENDING_S live in `services.heartbeat` — the inspector's
# projection mirrors them, so there is one drift-free source.
_MAX_CHECKINS_PER_STEP = 25

# Consecutive-failed-check-in backoff (Task #1928): a check-in that produces no
# LLM turn is a failed check-in — the agent is wedged (the 3962 context-overflow
# case: 1146 nudges against a permanently-rejecting provider, evidence #1289)
# and every nudge just re-fires the doomed call. The daemon tracks the streak
# per agent in-process and spaces the next check-in by `2^streak` idle windows,
# so a broken agent stops being poked on the normal cadence while staying
# recoverable: the first check-in after the backoff window is a probe, and a
# real turn (last_active_at advancing, or fresh activity from a real wake)
# resets the streak. In-process state only: a daemon restart re-probes at the
# normal cadence, and the agent-side circuit breaker (open = check-ins consumed
# without an LLM call) keeps the doomed calls off meanwhile.
# streak=1 -> next check-in after 2 idle windows; the cap bounds the longest
# silence (~5.3h at a 5min idle threshold).
_BACKOFF_MAX_WINDOWS = 64
# Platform-side nudge backoff (B7): a no-op nudge — no real inbound arrived and
# the agent did not pause — raises the agent's persisted backoff level, which
# stretches the reminder floor to heartbeat_interval * 2^level, capped at 24h.
# The level lives in agents_meta.heartbeat_backoff_level (survives daemon
# restarts); the consecutive-no-op counter is in-process only, so a restart
# recounts from zero. Real inbound or a pause resets the level to 0.
_BACKOFF_MAX_INTERVAL_S = 86400
_BACKOFF_MAX_LEVEL = 16  # schema CHECK bound; the raise-time cap is tighter


def _backoff_max_level(interval_s: float) -> int:
    """Highest backoff level whose stretched interval stays under the 24h cap."""
    if interval_s <= 0:
        return 0
    return max(0, math.floor(math.log2(_BACKOFF_MAX_INTERVAL_S / interval_s)))


# An idle_minutes reading this much below the value recorded at check-in time
# counts as "the check-in produced a turn" (slack absorbs clock skew).
_ADVANCE_SLACK_MINUTES = 0.5

# Heartbeat note delivered as a system note (kind='heartbeat') so the
# agent sees it as a framework-level marker, not an ordinary chat message.
# The claim node wraps it via system_note_message(tag=NoteTag.HEARTBEAT).


def _select_idle_agents_needing_heartbeat(
    pool: ConnectionPool,
    idle_threshold_s: float,
    *,
    heartbeat_interval_s: float | None = None,
    jitter_span_s: float = 0.0,
    limit: int | None = None,
    backoff_until: dict[int, float] | None = None,
) -> list[tuple[int, float]]:
    """Cluster-wide idle agents due for a heartbeat check-in, each as
    `(agent_id, idle_minutes)`, oldest-idle first.

    The idle clock is `last_active_at` — the timestamp of the agent's last
    completed LLM turn (real work), NOT `status_changed_at`. status_changed_at is
    bumped by every status flip including ops lifecycle churn (rollout quiesce /
    respawn / update cycles an agent idling -> restarting -> ... -> idling),
    so keying idle time off it let an ops restart reset the whole fleet's idle
    timers. last_active_at is written only by a real turn and is untouched by that
    cycle (an idle agent runs no LLM turn through it), so an ops event never resets
    an agent's idle timer.

    An agent is due when it is `idling`, has no pending inbound already queued
    to wake it (one about to wake on a real message does not also need a
    check-in), and `now()` has reached its next check-in time. Its next
    check-in is the later of the pause window, idle clock, and durable reminder
    clock: `GREATEST(heartbeat_paused_until, last_active_at +
    idle_threshold_s + jitter, last_heartbeat_at + heartbeat_interval_s)`. The
    reminder floor starts in the same transaction as the inbound insert, so a
    check-in that is consumed without producing an LLM turn cannot be re-added
    every dispatch step. The pause window is a floor; while it dominates, no
    check-in can arrive before its end. PostgreSQL `GREATEST` ignores a NULL
    pause or reminder timestamp, preserving the existing behavior for agents
    never reminded and pre-migration rows.

    `jitter_span_s` de-phases the idle-clock term by a deterministic per-agent
    offset `id mod jitter_span_s` seconds, spreading a fleet that went idle
    together across a `jitter_span_s`-wide window so it does not come due (and
    wake) in one batch. Deterministic on `id`, so it survives across cycles and
    breaks the self-synchronization the check-in itself would otherwise induce. `0`
    (the default) disables jitter — the `NULLIF` guards the `mod` against a
    divide-by-zero and collapses the offset to 0. Jitter affects only the
    idle-clock term; while the pause floor dominates, there is no jitter. `limit`
    caps the batch (the hard per-step wake-rate ceiling); with oldest-idle-first
    ordering the most overdue agents drain first. Both default to the
    un-jittered, unlimited behaviour so existing timing tests read the raw
    predicate.

    No machine filter: the inbound-insert trigger wakes the agent wherever it
    runs.

    An idle agent has no running turn task and needs no live lease to receive
    a check-in. The host dispatcher starts its next turn from the durable wake.
    """
    # Direct callers that inspect the raw idle predicate retain its historic
    # threshold cadence; the daemon supplies its configured check-in interval.
    reminder_interval_s = idle_threshold_s if heartbeat_interval_s is None else heartbeat_interval_s

    sql = (
        "SELECT id, EXTRACT(EPOCH FROM (now() - last_active_at)) / 60.0 AS idle_minutes "
        "FROM agents_meta "
        "WHERE status = 'idling' "
        "AND now() >= GREATEST("
        "  heartbeat_paused_until, "
        "  last_active_at "
        "  + make_interval(secs => %s + COALESCE(mod(id, NULLIF(%s, 0)::int), 0)), "
        "  last_heartbeat_at "
        "  + make_interval(secs => LEAST(%s * power(2.0, heartbeat_backoff_level), 86400))"
        ") "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM inbound_messages im "
        "  WHERE im.agent_id = agents_meta.id AND im.status = 'pending' "
        "    AND im.created_at >= now() - make_interval(secs => %s) "
        ") "
        "ORDER BY last_active_at ASC"
    )
    params: list[object] = [
        idle_threshold_s,
        jitter_span_s,
        reminder_interval_s,
        STALE_PENDING_S,
    ]
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    # Consecutive-failure backoff: skip agents whose backoff deadline (monotonic
    # wall clock) has not arrived — a wedged agent must not be poked on the
    # normal cadence. The deadline dict is empty in the common case, so the
    # filter is a fast no-op. The limit is applied AFTER the backoff filter so
    # backed-off agents never consume the per-step wake-rate slots.
    if backoff_until:
        now = time.time()
        rows = [r for r in rows if now >= backoff_until.get(r[0], 0.0)]
    if limit is not None:
        rows = rows[:limit]
    return rows


def _send_heartbeat_checkin(pool: ConnectionPool, agent_id: int, idle_minutes: float) -> None:
    """INSERT one `heartbeat` inbound for `agent_id`, then publish a Redis wake so
    the (idle-by-selection) target picks it up now instead of at its next inbound-
    wait SELECT recheck. Delivered as a system note (kind='heartbeat') — the claim
    node wraps it via system_note_message(tag=NoteTag.HEARTBEAT). `idle_minutes`
    rides the telemetry event only; the inbound content is the plain check-in."""
    content = "Heartbeat. Find something to do, or pause your heartbeat for some time."
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source) "
            "VALUES (%s, %s, 'heartbeat', 'system')",
            (agent_id, content),
        )
        # This must share the inbound transaction: once the inbound is
        # committed, a consumed no-LLM heartbeat still has a durable cadence
        # floor even after a daemon restart loses its in-memory backoff state.
        cur.execute("UPDATE agents_meta SET last_heartbeat_at = now() WHERE id = %s", (agent_id,))
        # The event name 'heartbeat_nudged' is stored row data in the events
        # table — renaming it would strand the existing history. Emit through
        # the unified pipeline (the events table).
        #
        # ts time-source note: the emitter stamps datetime.now(UTC) at ENQUEUE
        # time (process clock, one time source for the whole stream) — the old
        # direct agent_events INSERT used DB now() (transaction time). The two
        # can differ by the drain interval (≤0.5s) plus any queue backlog;
        # historical rows keep their original DB-clock ts (W7 rewiring).
        telemetry.emit(
            "telemetry",
            "heartbeat_nudged",
            level="info",
            agent_id=agent_id,
            source="system",
            attributes={"idle_minutes": round(idle_minutes)},
        )
    # The inbound is committed on `with` exit (the emit above is enqueued and
    # lands on the emitter's next batch — best-effort, JSONL-mirrored). The wake
    # is published after the inbound row is durable. Best-effort wake (see
    # shared.db.publish_inbound_wake); heartbeat carries no user-facing inbound
    # id, so "0".
    shared.db.publish_inbound_wake(agent_id, "0")


def _reconcile_checkin_outcomes(
    pool: ConnectionPool,
    *,
    pending_checkin: dict[int, float],
    failure_streak: dict[int, int],
    idle_threshold_s: float,
    noop_streak: dict[int, int] | None = None,
    heartbeat_interval_s: float = 300.0,
    noop_nudges_threshold: int | None = None,
) -> None:
    """Judge the previous cycle's check-ins and detect recovery, updating the
    per-agent failure streak and the B7 no-op-nudge streak.

    For every tracked agent (a check-in sent last cycle, or already on a
    streak):

    - the check-in advanced `last_active_at` (the agent ran a real turn) →
      streak reset;
    - `last_active_at` is now fresh (a real wake produced a turn without this
      daemon's nudge — the agent recovered on its own) → streak reset;
    - a sent check-in produced no turn → streak += 1 (the next check-in is
      spaced by `2^streak` idle windows);
    - the agent left the daemon's lanes (terminated / missing) → stop tracking.

    B7 (platform-side nudge backoff) rides the same pass: a sent check-in that
    produced neither a real inbound nor an agent pause increments `noop_streak`;
    at `noop_nudges_threshold` consecutive no-ops the persisted
    `heartbeat_backoff_level` is raised by one (the reminder floor stretches to
    `heartbeat_interval * 2^level`, capped at 24h) and the counter restarts. A
    real inbound or a pause clears the streak (the level reset itself is the
    `_sweep_backoff_resets` pass — it also covers agents this daemon is not
    tracking).

    `pending_checkin` maps agent_id → the `idle_minutes` observed when its
    check-in was sent; comparing `idle_minutes` readings (both derived from the
    DB clock) avoids wall-clock drift between this process and Postgres.
    """
    noop_streak = {} if noop_streak is None else noop_streak
    threshold = (
        settings.daemon.heartbeat_backoff_consecutive_noop_nudges
        if noop_nudges_threshold is None
        else noop_nudges_threshold
    )
    tracked = set(pending_checkin) | set(failure_streak) | set(noop_streak)
    if not tracked:
        return
    with pool.connection() as conn, conn.cursor() as cur:
        for agent_id in tracked:
            cur.execute(
                "SELECT status, "
                "EXTRACT(EPOCH FROM (now() - last_active_at)) / 60.0, "
                "heartbeat_backoff_level, "
                "(heartbeat_paused_until IS NOT NULL AND heartbeat_paused_until > now()), "
                "(last_heartbeat_at IS NOT NULL AND EXISTS ("
                "  SELECT 1 FROM inbound_messages im "
                "  WHERE im.agent_id = agents_meta.id AND im.kind <> 'heartbeat' "
                "    AND im.created_at > agents_meta.last_heartbeat_at)) "
                "FROM agents_meta WHERE id = %s",
                (agent_id,),
            )
            row = cur.fetchone()
            sent_at = pending_checkin.pop(agent_id, None)
            if row is None or row[0] not in ("idling", "running"):
                # Gone, or parked outside the daemon's lanes — stop tracking.
                failure_streak.pop(agent_id, None)
                noop_streak.pop(agent_id, None)
                continue
            idle_minutes = row[1]
            level = int(row[2] or 0)
            paused = bool(row[3])
            real_inbound = bool(row[4])
            advanced = (
                sent_at is not None
                and idle_minutes is not None
                and idle_minutes < sent_at - _ADVANCE_SLACK_MINUTES
            )
            recovered = advanced or (
                idle_minutes is not None and idle_minutes < idle_threshold_s / 60.0
            )
            if recovered:
                failure_streak.pop(agent_id, None)
            elif sent_at is not None:
                failure_streak[agent_id] = failure_streak.get(agent_id, 0) + 1
            # Not pending and not recovered: keep the existing streak — the
            # backoff deadline just extends by another window.

            # B7 no-op nudge streak — independent of the failure streak above.
            if paused or real_inbound:
                noop_streak.pop(agent_id, None)
            elif sent_at is not None:
                noop_streak[agent_id] = noop_streak.get(agent_id, 0) + 1
                if noop_streak[agent_id] >= threshold:
                    noop_streak[agent_id] = 0
                    new_level = min(level + 1, _backoff_max_level(heartbeat_interval_s))
                    if new_level > level:
                        _raise_backoff_level(pool, agent_id, new_level, heartbeat_interval_s)


def _raise_backoff_level(
    pool: ConnectionPool, agent_id: int, new_level: int, interval_s: float
) -> None:
    """Persist a raised B7 backoff level and emit its event."""
    stretched_s = int(min(interval_s * (2**new_level), _BACKOFF_MAX_INTERVAL_S))
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET heartbeat_backoff_level = %s WHERE id = %s",
            (new_level, agent_id),
        )
        telemetry.emit(
            "telemetry",
            "heartbeat_backoff_raised",
            level="info",
            agent_id=agent_id,
            source="system",
            attributes={
                "level": new_level,
                "interval_seconds": stretched_s,
            },
        )


def _sweep_backoff_resets(pool: ConnectionPool) -> None:
    """Reset B7 backoff levels whose agent received real inbound or paused.

    Covers agents the daemon is not currently tracking (a fresh resurrect /
    first real message after a restart), so a stretched reminder interval never
    outlives the engagement that should end it.
    """
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, heartbeat_backoff_level, "
            "(heartbeat_paused_until IS NOT NULL AND heartbeat_paused_until > now()), "
            "(last_heartbeat_at IS NOT NULL AND EXISTS ("
            "  SELECT 1 FROM inbound_messages im "
            "  WHERE im.agent_id = agents_meta.id AND im.kind <> 'heartbeat' "
            "    AND im.created_at > agents_meta.last_heartbeat_at)) "
            "FROM agents_meta WHERE heartbeat_backoff_level > 0"
        )
        rows = cur.fetchall()
    for agent_id, level, paused, real_inbound in rows:
        if not paused and not real_inbound:
            continue
        with write_transaction(pool) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET heartbeat_backoff_level = 0 WHERE id = %s",
                (agent_id,),
            )
            telemetry.emit(
                "telemetry",
                "heartbeat_backoff_reset",
                level="info",
                agent_id=agent_id,
                source="system",
                attributes={
                    "previous_level": int(level),
                    "reason": "paused" if paused else "real_inbound",
                },
            )


def _backoff_deadlines(failure_streak: dict[int, int], idle_threshold_s: float) -> dict[int, float]:
    """Monotonic-wall-clock deadline (seconds) before which each streaking agent
    must not be checked in on: `now + min(2^streak, _BACKOFF_MAX_WINDOWS) *
    idle_threshold`. A streak of 1 doubles the normal interval; the cap bounds
    the longest silence (~5.3h at a 5min threshold) so the daemon still probes
    a wedged agent occasionally."""
    now = time.time()
    return {
        agent_id: now + min(2**streak, _BACKOFF_MAX_WINDOWS) * idle_threshold_s
        for agent_id, streak in failure_streak.items()
    }


def _write_pidfile() -> None:
    if not acquire_pidfile(_PIDFILE, "services.heartbeat.daemon"):
        _log.info("[heartbeat] daemon already running (pidfile=%s), exiting", _PIDFILE)
        sys.exit(1)


def _remove_pidfile() -> None:
    remove_pidfile(_PIDFILE)


def _is_running() -> bool:
    """Whether a daemon is already running (via pidfile, new + legacy paths).

    Pid-reuse-safe: a live pid whose argv does not name this daemon's module
    is a recycled pid, not a running instance (audit round 2, P1)."""
    return pidfile_holds_daemon(_PIDFILE, "services.heartbeat.daemon") or pidfile_holds_daemon(
        legacy_pid_path("heartbeat"), "services.heartbeat.daemon"
    )


async def _sleep_with_liveness(liveness: Liveness, total_s: float) -> None:
    """Sleep `total_s`, beating liveness every `_LIVENESS_BEAT_STEP_S` so a long
    inter-poll wait keeps /healthz fresh instead of reading as a wedged loop."""
    remaining = total_s
    while remaining > 0:
        liveness.beat()
        step = min(_LIVENESS_BEAT_STEP_S, remaining)
        await asyncio.sleep(step)
        remaining -= step


async def _dispatch_loop(pool: ConnectionPool, liveness: Liveness) -> None:
    """Main loop: on bounded dispatch steps, send a check-in to due idle agents
    that have not paused.

    Idle agents that ignore the check-in keep getting one each cycle — it is the
    safety net; an agent that is truly waiting opts out with
    `ava.self.pause_heartbeat()`, and one that is truly done terminates.

    Dispatch runs on a fine `step` cadence (min of the configured
    `heartbeat_interval_seconds` and `_DISPATCH_STEP_S`) so due agents drain in
    small time-slices; each step checks in on at most `_MAX_CHECKINS_PER_STEP` of
    them, and the due-time carries a per-agent jitter (`JITTER_SPAN_S`).
    Together these keep the fleet-wide wake rate bounded and de-synchronized —
    see the module-level "Wakeup-storm flattening" note.
    """
    idle_threshold = settings.daemon.heartbeat_idle_threshold_seconds
    heartbeat_interval = settings.daemon.heartbeat_interval_seconds
    step = min(settings.daemon.heartbeat_interval_seconds, _DISPATCH_STEP_S)
    _log.info(
        "[heartbeat] daemon started, pid=%s, step=%.0fs, idle_threshold=%.0fs, "
        "jitter_span=%.0fs, max_checkins_per_step=%d (wake-rate ceiling ~%.2f/s)",
        os.getpid(),
        step,
        idle_threshold,
        JITTER_SPAN_S,
        _MAX_CHECKINS_PER_STEP,
        _MAX_CHECKINS_PER_STEP / step,
    )
    # Consecutive-failure backoff state (Task #1928): per-agent failure streaks
    # and the idle_minutes observed at each sent check-in. In-process only — a
    # daemon restart re-probes everyone at the normal cadence.
    pending_checkin: dict[int, float] = {}
    failure_streak: dict[int, int] = {}
    # B7 no-op-nudge counter: in-process only; the raised level itself persists
    # in agents_meta.heartbeat_backoff_level.
    noop_streak: dict[int, int] = {}
    while True:
        try:
            await _sleep_with_liveness(liveness, step)
            _sweep_backoff_resets(pool)
            _reconcile_checkin_outcomes(
                pool,
                pending_checkin=pending_checkin,
                failure_streak=failure_streak,
                idle_threshold_s=idle_threshold,
                noop_streak=noop_streak,
                heartbeat_interval_s=heartbeat_interval,
            )
            rows = _select_idle_agents_needing_heartbeat(
                pool,
                idle_threshold,
                heartbeat_interval_s=heartbeat_interval,
                jitter_span_s=JITTER_SPAN_S,
                limit=_MAX_CHECKINS_PER_STEP,
                backoff_until=_backoff_deadlines(failure_streak, idle_threshold),
            )
            for agent_id, idle_minutes in rows:
                try:
                    _send_heartbeat_checkin(pool, agent_id, idle_minutes)
                    pending_checkin[agent_id] = idle_minutes
                    _log.info(
                        "[heartbeat] checked in on idle agent %s (idle %.0f min)",
                        agent_id,
                        idle_minutes,
                    )
                except Exception as exc:
                    _log.error("[heartbeat] check-in for agent %s failed: %r", agent_id, exc)
        except asyncio.CancelledError:
            raise
        except psycopg.ProgrammingError:
            _log.critical(
                "[heartbeat] schema / syntax error — code<->DB drift; retry will not self-heal, daemon exiting, restart after fix",
                exc_info=True,
            )
            raise
        except Exception:
            _log.exception("[heartbeat] poll iteration failed")


async def _liveness_loop(pool: ConnectionPool, liveness: Liveness) -> None:
    """Slow loop running the agent-liveness pass (Task #1174) — see
    `services.heartbeat.liveness`. Catches per-pass failures so one bad pass
    (e.g. a DB blip) never takes down the daemon; the next pass retries."""
    while True:
        try:
            await _sleep_with_liveness(liveness, _PASS_INTERVAL_S)
            await run_liveness_pass(pool)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("[heartbeat] liveness pass failed")


async def run() -> None:
    """Start the daemon: healthz server -> write pidfile -> connect DB -> enter main loop."""
    if _is_running():
        _log.info("[heartbeat] daemon already running (pidfile=%s), exiting", _PIDFILE)
        sys.exit(1)

    # Publish the pidfile before binding healthz so identity-aware probes can verify it.
    _write_pidfile()
    _log.info("[heartbeat] pidfile written: %s", _PIDFILE)

    liveness = Liveness(_LIVENESS_TIMEOUT_S)
    health = await start_health_server("heartbeat", liveness=liveness)
    _log.info("[heartbeat] healthz listening on :%s", health_port("heartbeat"))

    pool = shared.db.pool()
    # Liveness pass (Task #1174): a slow independent task alongside the check-in
    # loop, so a stalled probe fan-out (bounded by _PROBE_TIMEOUT_S) can never
    # delay a check-in. One pass per _PASS_INTERVAL_S, first pass after one full
    # interval (the DB merge is cheap; there is nothing to judge before the
    # first probe anyway).
    liveness_task = asyncio.create_task(_liveness_loop(pool, liveness))
    try:
        await _dispatch_loop(pool, liveness)
    finally:
        liveness_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await liveness_task
        pool.close()
        await stop_health_server(health)
        _remove_pidfile()
        _log.info("[heartbeat] daemon stopped")


def main() -> None:
    """Entry point: init logger + run asyncio loop.

    SIGTERM (the graceful stop `ava cluster update` sends) and Ctrl-C converge on
    the same `KeyboardInterrupt` unwind — see `shared.daemon_shutdown`. `ava stop`
    default force-kill does not reach this.
    """
    from shared.migrations import assert_schema_current

    # Pre-startup sanity: schema version must match code; raises SchemaVersionMismatch if not.
    assert_schema_current(settings.data_plane.db_url)
    init_gateway_process(name="heartbeat")
    install_graceful_shutdown("heartbeat")
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        _log.info("[heartbeat] interrupted, shutting down")
    except Exception:
        _log.exception("[heartbeat] daemon crashed — uncaught exception escaped run()")
        raise
    finally:
        _remove_pidfile()


if __name__ == "__main__":
    main()
