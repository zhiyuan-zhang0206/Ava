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

Kept alive by the root supervisor's health monitor through the roster's `/healthz`
identity probe (`ops/roster/healthz.py`).
"""

import asyncio
import logging
import math
import os
import signal
import sys
import time
from pathlib import Path

import psycopg
from psycopg_pool import ConnectionPool

import base.db
from base import telemetry
from base.config import settings
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
from services.heartbeat import JITTER_SPAN_S, STALE_PENDING_S, completion_digest
from services.heartbeat.liveness import _PASS_INTERVAL_S, run_liveness_pass
from services.pidfile import acquire_pidfile, pidfile_holds_daemon, remove_pidfile

_log = logging.getLogger("services.heartbeat.daemon")


def _endpoint() -> ServiceEndpoint:
    return ServiceEndpoints.from_settings().of("heartbeat")


def _pidfile() -> Path:
    return _endpoint().pidfile


# Liveness staleness ceiling. The loop sleeps a long inter-poll interval (default
# 300s), so `_sleep_with_liveness` beats every _LIVENESS_BEAT_STEP_S during that
# wait; the ceiling only has to exceed that step, not the whole interval. A
# genuinely wedged loop (a beat step that never returns) still trips /healthz 503
# -> watchdog respawn.
_LIVENESS_TIMEOUT_S = 60.0
_LIVENESS_BEAT_STEP_S = 30.0
# One digest round is a bounded read plus one delivery per completed agent-hour; the
# loop also beats during its 60 s waits.
_DIGEST_LIVENESS_TIMEOUT_S = 300.0

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
    respawn / update cycles an agent through idling and back),
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

    # EXTRACT returns numeric (Decimal); keep the observed clock compatible
    # with the float slack used when the next dispatch reconciles this reading.
    sql = (
        "SELECT id, (EXTRACT(EPOCH FROM (now() - last_active_at)) / 60.0)::double precision "
        "AS idle_minutes "
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
    # Consecutive-failure backoff: skip agents whose absolute backoff deadline
    # has not arrived — a wedged agent must not be poked on the normal cadence.
    # The deadline dict is empty in the common case, so the
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
        # The event name 'heartbeat_nudged' is stored row data — renaming it
        # would strand the existing history. Emit through the unified pipeline
        # (`base/telemetry/emitter.py`).
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
    # base.db.publish_inbound_wake); heartbeat carries no user-facing inbound
    # id, so "0".
    base.db.publish_inbound_wake(agent_id, "0")


def _reconcile_agent(
    pool: ConnectionPool,
    cur: psycopg.Cursor,
    agent_id: int,
    *,
    pending_checkin: dict[int, float],
    failure_streak: dict[int, int],
    noop_streak: dict[int, int],
    idle_threshold_s: float,
    heartbeat_interval_s: float,
    threshold: int,
) -> None:
    """Judge one tracked agent's last check-in: update its failure and no-op streaks."""
    cur.execute(
        "SELECT status, "
        "(EXTRACT(EPOCH FROM (now() - last_active_at)) / 60.0)::double precision, "
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
        return
    idle_minutes = row[1]
    advanced = (
        sent_at is not None
        and idle_minutes is not None
        and idle_minutes < sent_at - _ADVANCE_SLACK_MINUTES
    )
    recovered = advanced or (idle_minutes is not None and idle_minutes < idle_threshold_s / 60.0)
    if recovered:
        failure_streak.pop(agent_id, None)
    elif sent_at is not None:
        failure_streak[agent_id] = failure_streak.get(agent_id, 0) + 1
    # Not pending and not recovered: keep the existing streak and
    # its previously assigned backoff deadline.

    # B7 no-op nudge streak — independent of the failure streak above.
    if bool(row[3]) or bool(row[4]):
        noop_streak.pop(agent_id, None)
    elif sent_at is not None:
        _count_noop_nudge(
            pool, agent_id, int(row[2] or 0), noop_streak, heartbeat_interval_s, threshold
        )


def _count_noop_nudge(
    pool: ConnectionPool,
    agent_id: int,
    level: int,
    noop_streak: dict[int, int],
    heartbeat_interval_s: float,
    threshold: int,
) -> None:
    """A check-in with no real inbound and no pause: count it, and at `threshold` raise the level."""
    noop_streak[agent_id] = noop_streak.get(agent_id, 0) + 1
    if noop_streak[agent_id] >= threshold:
        noop_streak[agent_id] = 0
        new_level = min(level + 1, _backoff_max_level(heartbeat_interval_s))
        if new_level > level:
            _raise_backoff_level(pool, agent_id, new_level, heartbeat_interval_s)


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
            _reconcile_agent(
                pool,
                cur,
                agent_id,
                pending_checkin=pending_checkin,
                failure_streak=failure_streak,
                noop_streak=noop_streak,
                idle_threshold_s=idle_threshold_s,
                heartbeat_interval_s=heartbeat_interval_s,
                threshold=threshold,
            )


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


def _backoff_deadlines(
    failure_streak: dict[int, int],
    idle_threshold_s: float,
    deadline_state: dict[int, tuple[int, float]] | None = None,
) -> dict[int, float]:
    """Keep each absolute deadline until its agent's failure streak changes.

    A new streak gets `now + min(2^streak, _BACKOFF_MAX_WINDOWS) *
    idle_threshold`. A streak of 1 doubles the normal interval; the cap bounds
    the longest silence (~5.3h at a 5min threshold) so the daemon still probes
    a wedged agent occasionally. The state is in-process only.
    """
    deadline_state = {} if deadline_state is None else deadline_state
    for agent_id in deadline_state.keys() - failure_streak.keys():
        del deadline_state[agent_id]
    now = time.time()
    for agent_id, streak in failure_streak.items():
        previous = deadline_state.get(agent_id)
        if previous is None or previous[0] != streak:
            deadline_state[agent_id] = (
                streak,
                now + min(2**streak, _BACKOFF_MAX_WINDOWS) * idle_threshold_s,
            )
    return {agent_id: deadline for agent_id, (_, deadline) in deadline_state.items()}


def _write_pidfile() -> None:
    if not acquire_pidfile(_pidfile(), "services.heartbeat.daemon"):
        _log.info("[heartbeat] daemon already running (pidfile=%s), exiting", _pidfile())
        sys.exit(1)


def _remove_pidfile() -> None:
    remove_pidfile(_pidfile())


def _is_running() -> bool:
    """Whether a daemon is already running (via its pidfile).

    Pid-reuse-safe: a live pid whose argv does not name this daemon's module
    is a recycled pid, not a running instance (audit round 2, P1)."""
    return pidfile_holds_daemon(_pidfile(), "services.heartbeat.daemon")


async def _sleep_with_liveness(liveness: LoopProgress, total_s: float) -> None:
    """Sleep `total_s`, beating liveness every `_LIVENESS_BEAT_STEP_S` so a long
    inter-poll wait keeps /healthz fresh instead of reading as a wedged loop."""
    remaining = total_s
    while remaining > 0:
        liveness.beat()
        step = min(_LIVENESS_BEAT_STEP_S, remaining)
        await asyncio.sleep(step)
        remaining -= step


async def _dispatch_loop(pool: ConnectionPool, liveness: LoopProgress) -> None:
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
    deadline_state: dict[int, tuple[int, float]] = {}
    # B7 no-op-nudge counter: in-process only; the raised level itself persists
    # in agents_meta.heartbeat_backoff_level.
    noop_streak: dict[int, int] = {}
    while True:
        try:
            await _sleep_with_liveness(liveness, step)
            if admission.quiesced():
                continue
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
                backoff_until=_backoff_deadlines(failure_streak, idle_threshold, deadline_state),
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


async def _liveness_loop(
    db: Database, pool: ConnectionPool, bus: EventBus, liveness: LoopProgress
) -> None:
    """Run agent-liveness checks, the first at start so the roster read model is
    populated at once; a failed pass is retried on the next interval."""
    while True:
        try:
            if not admission.quiesced():
                await run_liveness_pass(db, pool, bus)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("[heartbeat] liveness loop iteration failed")
        await _sleep_with_liveness(liveness, _PASS_INTERVAL_S)


async def run() -> None:
    """Start the daemon: healthz server -> write pidfile -> connect DB -> enter main loop."""
    if _is_running():
        _log.info("[heartbeat] daemon already running (pidfile=%s), exiting", _pidfile())
        sys.exit(1)

    # Publish the pidfile before binding healthz so identity-aware probes can verify it.
    _write_pidfile()
    _log.info("[heartbeat] pidfile written: %s", _pidfile())

    liveness = LivenessGroup()
    dispatch_progress = liveness.register("dispatch", _LIVENESS_TIMEOUT_S)
    liveness_progress = liveness.register("liveness", _LIVENESS_TIMEOUT_S)
    digest_progress = liveness.register("completion_digest", _DIGEST_LIVENESS_TIMEOUT_S)
    endpoint = _endpoint()
    health = await start_health_server("heartbeat", endpoint.health_port, liveness=liveness)
    _log.info("[heartbeat] healthz listening on :%s", endpoint.health_port)

    db = Database.from_settings()
    pool = db.pool()
    bus = EventBus.from_settings()
    try:
        # One TaskGroup owns the resident loops, each with its own progress tracker
        # so a stalled loop cannot be masked by a busy sibling. The liveness pass
        # (Task #1174) is a slow independent loop beside the check-in loop, so a
        # stalled probe fan-out (bounded by _PROBE_TIMEOUT_S) can never delay a
        # check-in; the completion digest is a third. A loop that raises cancels
        # its siblings and ends the process, and the supervisor restarts it.
        async with asyncio.TaskGroup() as loops:
            loops.create_task(_dispatch_loop(pool, dispatch_progress))
            loops.create_task(_liveness_loop(db, pool, bus, liveness_progress))
            loops.create_task(
                completion_digest.completion_digest_loop(pool, db, bus, digest_progress)
            )
    finally:
        pool.close()
        await stop_health_server(health)
        _remove_pidfile()
        _log.info("[heartbeat] daemon stopped")


def main() -> None:
    """Entry point: init logger + run asyncio loop.

    SIGTERM (the graceful stop the fleet update sends) and Ctrl-C converge on
    the same `KeyboardInterrupt` unwind — see `base.daemon.shutdown`. `ava stop`
    default force-kill does not reach this.
    """
    from base.deploy.schema.migrations import assert_schema_current

    # Pre-startup sanity: schema version must match code; raises SchemaVersionMismatch if not.
    assert_schema_current(settings.data_plane.db_url)
    init_gateway_process(name="heartbeat")
    install_graceful_shutdown("heartbeat")
    code = 0
    # `asyncio.Runner`, not `asyncio.run`: `run` closes in a `finally` that
    # awaits `shutdown_default_executor`, joining the default executor's
    # workers — a stranded-hold grading pass among them — and a stop signal
    # must never wait on those (see `_hard_exit`). The runner is therefore
    # never closed: after the explicit drain below, teardown is skipped by the
    # hard exit.
    runner = asyncio.Runner()
    try:
        runner.run(run())
    except KeyboardInterrupt:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)  # a retry must not abort the bounded exit
        _log.info("[heartbeat] interrupted, shutting down")
        # The signal path skips Runner's own cancellation, so drain the loop's
        # tasks explicitly: run()'s finally still cancels the liveness loop,
        # closes the pool and stops the health server. The executor is
        # deliberately NOT drained.
        failures = cancel_and_drain(runner)
        if failures:
            _log.error("[heartbeat] async shutdown failed: %r", failures)
            code = 1
    except Exception:
        _log.exception("[heartbeat] daemon crashed — uncaught exception escaped run()")
        code = 1
    finally:
        _remove_pidfile()
    _hard_exit(code)


if __name__ == "__main__":
    main()
