"""Task-maintenance daemon — gateway-owned task reminders + escalation.

Fleet-domain gateway daemon, registered into the ops service roster by the
`ava_fleet` plugin (`plugins/ava_fleet/services.py`) rather than hardcoded in
`ops/spec.py`: the whole remind/escalate surface is fleet, so it lives under the
plugin namespace and is discovered only when the plugin is enabled.

One gateway-side loop, cluster-wide. Reminders and escalations directly insert
into `inbound_messages` and then best-effort wake via Redis, modeled on heartbeat.
They must not resurrect terminated agents: per the 2026-08-22 user ruling, an old
agent remains terminated with its reminder unclaimed while the task escalates;
whoever takes over spawns a new agent. Counter updates stay direct DB writes.

- Remind: every `AVA_TASK_MAINTENANCE_INTERVAL_SECONDS` (default 5 min), find
  in-progress tasks whose owner has not touched them within their
  `remind_interval_seconds` window and deliver one chat digest per owner. An
  overdue window repeats at max(backoff, remind_interval_seconds), so a P3 task
  (4h interval) is not nagged hourly; `last_reminded_at` gates it. A failed
  task-counter write is retried without re-delivering (same-cause dedup).
- Escalate: when `reminder_count` reaches `AVA_TASK_ESCALATE_N` (default 3),
  notify the parent task's owner (the delegator) that the current owner is
  unresponsive. A top-level task has no delegating parent owner (its parent is
  the ownerless system root), so it escalates to the user instead
  — a require_response notice posted on the stalled owner that surfaces in the
  human queue, grouped under the task.

No stale sweep, no automatic cancellation, no orphan release. The system only
speaks — posting a notice or a message; it never changes task state.

Usage:
    .venv/bin/python -m ava_builtins.plugins.ava_fleet.task_maintenance.daemon

Kept alive by the gateway watchdog's 60s healthcheck
(`ava_builtins/plugins/ava_fleet/task_maintenance/healthcheck.py`), wired via
the plugin's `services()` ServiceSpec.healthcheck_module — so the schema-drift
exit in `_dispatch_loop` is revived on the next round instead of staying dead.
"""

import asyncio
import logging
import os
import sys
import time
from collections import defaultdict

import psycopg
from psycopg_pool import ConnectionPool

import shared.db
from services._pidfile import acquire_pidfile, pidfile_holds_daemon, remove_pidfile
from shared import telemetry
from shared.config import settings
from shared.daemon_health import (
    Liveness,
    health_port,
    start_health_server,
    stop_health_server,
)
from shared.daemon_shutdown import install_graceful_shutdown
from shared.live_announce import publish_agent_updated_sync
from shared.log import init_gateway_process
from shared.paths import legacy_pid_path

_log = logging.getLogger("ava_builtins.plugins.ava_fleet.task_maintenance.daemon")

_PIDFILE = settings.services.task_maintenance_pidfile
_LIVENESS_TIMEOUT_S = 60.0
_LIVENESS_BEAT_STEP_S = 30.0


def _deliver_message(pool: ConnectionPool, agent_id: int, message: str) -> None:
    """Insert a system chat inbound, commit it, then wake its agent.

    Direct delivery intentionally cannot resurrect a terminated agent; its inbox
    row remains inspectable while escalation directs the work onward."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source) "
            "VALUES (%s, %s, 'chat', 'system') RETURNING id",
            (agent_id, message),
        )
        inbound_id = int(cur.fetchone()[0])  # type: ignore[index]
    # The connection context commits before the best-effort wake. A missing
    # subscriber is expected for a terminated agent and does not resurrect it.
    shared.db.publish_inbound_wake(agent_id, str(inbound_id))


# ── Reminder pass ──────────────────────────────────────────────────────────────

# Tasks whose remind_interval_seconds has elapsed AND the owner hasn't been reminded
# within this overdue window (last_reminded_at gates it within
# max(backoff, remind_interval_seconds) — each task repeats at its own cadence).
# Terminated owners remain eligible: direct delivery records without reviving them.
_REMINDER_SQL = """
    SELECT id, owner, title, remind_interval_seconds,
           EXTRACT(EPOCH FROM (now() - updated_at))::bigint AS elapsed
    FROM agent_tasks
    WHERE status = 'in_progress'
      AND owner IS NOT NULL
      AND remind_interval_seconds IS NOT NULL
      AND NOT is_root
      AND now() - updated_at > make_interval(secs => remind_interval_seconds)
      AND (
          last_reminded_at IS NULL
          OR now() - last_reminded_at > make_interval(secs => GREATEST(%s, remind_interval_seconds))
      )
"""


# Task ids whose reminder message was delivered but whose counter write
# failed (a DB blip after a 2xx delivery), mapped to the monotonic delivery
# time. While an entry is within _DELIVER_DEDUP_WINDOW_S, a sweep retries the
# counter write instead of re-delivering the message — same-cause dedup, so
# the owner gets one reminder, not a duplicate minutes later. Entries expire
# after the window: a task that went quiet again in a NEW overdue window gets
# a fresh reminder (the owner's update reset the counters in between), and
# expired entries are pruned each sweep so the dict stays bounded by the
# number of recently delivered tasks. In-memory only: a daemon restart
# forgets these, and the DB backoff floor (>= 1h) covers that gap.
_DELIVER_DEDUP_WINDOW_S = 900.0
_pending_counter_writes: dict[int, float] = {}


def _advance_reminder_counters(pool: ConnectionPool, task_id: int) -> None:
    """Advance last_reminded_at / reminder_count for a delivered reminder.

    Split out of _run_reminders so a sweep can retry this write without
    re-delivering the message (same-cause dedup). Raises on failure so the
    caller records the task in _pending_counter_writes."""
    with pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_tasks SET last_reminded_at = now(), reminder_count = reminder_count + 1 "
            "WHERE id = %s",
            (task_id,),
        )


def _reminder_digest_message(tasks: list[tuple[int, str, int, int]]) -> str:
    """Format one owner's overdue tasks without a single-task special case."""
    lines = [f"Task reminders — you have {len(tasks)} overdue task(s):"]
    for task_id, title, remind_interval_seconds, elapsed in tasks:
        lines.append(
            f'- #{task_id} "{title}" — idle {elapsed / 3600:.1f}h '
            f"(reminder interval: {remind_interval_seconds // 60}min)"
        )
    return "\n".join(lines)


def _run_reminders(pool: ConnectionPool, backoff_seconds: float) -> int:
    """Deliver one overdue-task digest per owner. Returns fully recorded digests."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(_REMINDER_SQL, (backoff_seconds,))
        overdue = cur.fetchall()

    sent = 0
    # Expire dedup marks past the window BEFORE the loop: a stale mark must
    # never suppress a legitimate reminder for a new overdue window (the
    # owner's update resets the counters, so the SELECT picks the task again).
    now_mono = time.monotonic()
    for marked_task_id, marked_at in list(_pending_counter_writes.items()):
        if now_mono - marked_at > _DELIVER_DEDUP_WINDOW_S:
            del _pending_counter_writes[marked_task_id]
    overdue_by_owner: dict[int, list[tuple[int, str, int, int]]] = defaultdict(list)
    for task_id, owner, title, remind_interval_seconds, elapsed in overdue:
        if task_id in _pending_counter_writes:
            # Delivered on an earlier sweep but the counter write failed, so
            # the backoff gate never advanced and the task is selected again.
            # Finish the bookkeeping instead of re-delivering the same
            # reminder minutes later (same-cause dedup).
            try:
                _advance_reminder_counters(pool, task_id)
            except Exception as exc:
                _log.error(
                    "[task-maintenance] reminder counters for task %s failed: %r",
                    task_id,
                    exc,
                )
                continue
            del _pending_counter_writes[task_id]
            _log.info(
                "[task-maintenance] recorded earlier reminder for task %s (counter retry)",
                task_id,
            )
            continue
        overdue_by_owner[owner].append((task_id, title, remind_interval_seconds, elapsed))
    for owner, tasks in overdue_by_owner.items():
        task_ids = [task_id for task_id, _, _, _ in tasks]
        try:
            # Deliver before counters, so a failed digest leaves every task eligible.
            _deliver_message(pool, owner, _reminder_digest_message(tasks))
        except Exception as exc:
            _log.error(
                "[task-maintenance] reminder digest for owner %s (tasks %s) failed: %r",
                owner,
                task_ids,
                exc,
            )
            continue
        telemetry.emit(
            "telemetry",
            "task_reminder_digest",
            agent_id=owner,
            source="system",
            attributes={"owner_id": owner, "task_count": len(tasks), "task_ids": task_ids},
        )
        counter_failed = False
        for task_id in task_ids:
            try:
                _advance_reminder_counters(pool, task_id)
                _log.info(
                    "[task-maintenance] recorded reminder digest for owner %s, task %s",
                    owner,
                    task_id,
                )
            except Exception as exc:
                # Retry this bookkeeping without re-delivering the digest.
                _pending_counter_writes[task_id] = time.monotonic()
                counter_failed = True
                _log.error(
                    "[task-maintenance] reminder counters for task %s failed after "
                    "delivery: %r — retrying without re-delivery next sweep",
                    task_id,
                    exc,
                )
        if not counter_failed:
            sent += 1
    return sent


# ── Escalate pass ────────────────────────────────────────────────────────────

# Tasks whose reminder_count reached (>=) the escalation threshold, joined to their
# parent's owner (the delegator). p.owner is NULL for a top-level task (its
# parent is the ownerless system root) — those escalate to the user instead of
# a delegator (see _run_escalate). The >= here surfaces every
# at-or-past-threshold task; _run_escalate then applies the per-branch gate (the
# delegator branch fires exactly at the threshold, the user branch is >= and
# retry-eligible until its notice is posted).
_ESCALATE_SQL = """
    SELECT t.id, t.title, t.owner, t.reminder_count, t.priority, p.owner AS parent_owner
    FROM agent_tasks t
    JOIN agent_tasks p ON p.id = t.parent_id
    WHERE t.status = 'in_progress'
      AND t.reminder_count >= %s
      AND NOT t.is_root
"""


def _escalate_to_user_queue(
    pool: ConnectionPool, task_id: int, owner: int, priority: str, title: str
) -> bool:
    """Surface a stalled top-level task in the human queue by posting a
    require_response notice on the stalled owner agent.

    The notice hangs off the owner agent but its audience is the user: a
    require_response notice rides the agent snapshot's notices_awaiting_response
    into the "needs response" queue, grouped under the task via `task_id`. It
    inherits the task's `priority`. Skipped (returns False) when the owner
    already has an open notice — the human already has that agent flagged, and
    the one-open-notice-per-agent invariant that ava.ui.notify keeps must hold;
    this also makes the escalation self-idempotent, since a later sweep sees the
    still-open notice and skips. Returns True when a notice was posted."""
    notice_title = (
        f'Task #{task_id} "{title}" stalled after repeated reminders — reassign or cancel'
    )
    notice_content = (
        f"Agent #{owner} has not updated this task after repeated reminders, and no "
        "delegating agent owns its parent to catch it. Reassign it to another agent, "
        "cancel it, or reply to remind the owner once more."
    )
    with pool.connection() as conn:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM agent_notices WHERE agent_id = %s AND resolved_at IS NULL LIMIT 1",
                (owner,),
            )
            if cur.fetchone() is not None:
                return False
            cur.execute(
                "INSERT INTO agent_notices "
                "(agent_id, local_id, task_id, title, content, priority, require_response, blocking) "
                "VALUES (%s, COALESCE((SELECT MAX(local_id) FROM agent_notices WHERE agent_id = %s), -1) + 1, "
                "%s, %s, %s, %s, TRUE, FALSE)",
                (owner, owner, task_id, notice_title, notice_content, priority),
            )
        # Refresh the snapshot so the queue shows the notice live — same publish
        # ava.ui.notify does after a require_response notice.
        publish_agent_updated_sync(conn, owner)
    return True


def _delegator_digest_message(tasks: list[tuple[int, str, int, int]]) -> str:
    """Format one delegator's stalled subtasks without a per-task path."""
    lines = ["Stalled subtasks — owner(s) unresponsive after repeated reminders:"]
    for task_id, title, owner, reminder_count in tasks:
        lines.append(f'- #{task_id} "{title}" — owner #{owner}, {reminder_count} reminders')
    return "\n".join(lines)


def _run_escalate(pool: ConnectionPool, escalate_n: int) -> int:
    """Escalate unresponsive subtask owners.

    A delegated subtask (its parent has an owner) escalates to that parent
    owner — the delegator — with a chat message. A top-level task (its parent
    is the ownerless system root, parent_owner NULL) has no delegator to catch
    it, so it escalates to the user: a require_response notice on the stalled
    owner that surfaces in the human queue."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(_ESCALATE_SQL, (escalate_n,))
        rows = cur.fetchall()

    escalated = 0
    stalled_by_delegator: dict[int, list[tuple[int, str, int, int]]] = defaultdict(list)
    for task_id, title, owner, reminder_count, priority, parent_owner in rows:
        try:
            if parent_owner is not None:
                # Delegated subtask -> tell the delegator, once, exactly at the
                # threshold crossing. A fire-and-forget chat has no persistent
                # marker, so the exact-equality gate is what stops re-notifying:
                # the next reminder pushes reminder_count past the threshold.
                if reminder_count != escalate_n:
                    continue
                stalled_by_delegator[parent_owner].append((task_id, title, owner, reminder_count))
            else:
                # Top-level task -> surface in the human queue. Use
                # >= (not ==): a sweep that cannot post yet (the owner already
                # holds an open notice) must retry on later sweeps rather than
                # permanently miss this overdue window once the counter climbs
                # past the threshold. The escalation notice itself is the
                # idempotency marker — _escalate_to_user_queue skips while one is
                # open, so >= never double-posts.
                if reminder_count < escalate_n:
                    continue
                if _escalate_to_user_queue(pool, task_id, owner, priority, title):
                    telemetry.emit(
                        "telemetry",
                        "task_escalation",
                        agent_id=owner,
                        source="system",
                        attributes={
                            "owner_id": owner,
                            "task_count": 1,
                            "task_ids": [task_id],
                            "leg": "user",
                        },
                    )
                    _log.info(
                        "[task-maintenance] escalated user task %s to the human queue "
                        "(owner %s unresponsive after %d reminders)",
                        task_id,
                        owner,
                        reminder_count,
                    )
                    escalated += 1
        except Exception as exc:
            _log.error(
                "[task-maintenance] escalation for task %s failed: %r",
                task_id,
                exc,
            )
    for delegator, tasks in stalled_by_delegator.items():
        task_ids = [task_id for task_id, _, _, _ in tasks]
        try:
            _deliver_message(pool, delegator, _delegator_digest_message(tasks))
            telemetry.emit(
                "telemetry",
                "task_escalation",
                agent_id=delegator,
                source="system",
                attributes={
                    "owner_id": delegator,
                    "task_count": len(tasks),
                    "task_ids": task_ids,
                    "leg": "delegator",
                },
            )
            _log.info(
                "[task-maintenance] escalated tasks %s to delegator %s",
                task_ids,
                delegator,
            )
            escalated += 1
        except Exception as exc:
            _log.error(
                "[task-maintenance] escalation digest for delegator %s (tasks %s) failed: %r",
                delegator,
                task_ids,
                exc,
            )
    return escalated


# ── Daemon lifecycle ─────────────────────────────────────────────────────────


def _write_pidfile() -> None:
    if not acquire_pidfile(_PIDFILE, "ava_builtins.plugins.ava_fleet.task_maintenance.daemon"):
        _log.info("[task_maintenance] daemon already running (pidfile=%s), exiting", _PIDFILE)
        sys.exit(1)


def _remove_pidfile() -> None:
    remove_pidfile(_PIDFILE)


def _is_running() -> bool:
    """Whether a daemon is already running (via pidfile, new + legacy paths).

    Pid-reuse-safe: a live pid whose argv does not name this daemon's module
    is a recycled pid, not a running instance (audit round 2, P1)."""
    return pidfile_holds_daemon(
        _PIDFILE, "ava_builtins.plugins.ava_fleet.task_maintenance.daemon"
    ) or pidfile_holds_daemon(
        legacy_pid_path("task_maintenance"),
        "ava_builtins.plugins.ava_fleet.task_maintenance.daemon",
    )


async def _sleep_with_liveness(liveness: Liveness, total_s: float) -> None:
    remaining = total_s
    while remaining > 0:
        liveness.beat()
        step = min(_LIVENESS_BEAT_STEP_S, remaining)
        await asyncio.sleep(step)
        remaining -= step


async def _dispatch_loop(pool: ConnectionPool, liveness: Liveness) -> None:
    interval = settings.daemon.task_maintenance_interval_seconds
    backoff_seconds = settings.daemon.task_reminder_backoff_seconds
    escalate_n = settings.daemon.task_escalate_n
    _log.info(
        "[task-maintenance] daemon started, pid=%s, interval=%.0fs, backoff=%.0fs, escalate_n=%d",
        os.getpid(),
        interval,
        backoff_seconds,
        escalate_n,
    )
    while True:
        try:
            _run_reminders(pool, backoff_seconds)
            _run_escalate(pool, escalate_n)
        except asyncio.CancelledError:
            raise
        except psycopg.ProgrammingError:
            _log.critical(
                "[task-maintenance] schema / syntax error — code<->DB drift; "
                "retry will not self-heal, daemon exiting, restart after fix",
                exc_info=True,
            )
            raise
        except Exception:
            _log.exception("[task-maintenance] poll iteration failed")
        # Sleep OUTSIDE the try: a transient failure waits a full interval
        # before retrying instead of hot-looping against Postgres (same
        # discipline as services/events_maintenance/daemon.py — audit
        # round 2, P1: the sleep used to sit inside the try, so a
        # non-ProgrammingError exception skipped it and the loop spun).
        await _sleep_with_liveness(liveness, interval)


async def run() -> None:
    if _is_running():
        _log.info(
            "[task-maintenance] daemon already running (pidfile=%s), exiting",
            _PIDFILE,
        )
        sys.exit(1)

    # Pidfile before the healthz bind — see services/restarter/daemon.py:run().
    _write_pidfile()
    _log.info("[task-maintenance] pidfile written: %s", _PIDFILE)

    liveness = Liveness(_LIVENESS_TIMEOUT_S)
    health = await start_health_server("task_maintenance", liveness=liveness)
    _log.info("[task-maintenance] healthz listening on :%s", health_port("task_maintenance"))

    pool = shared.db.pool()
    try:
        await _dispatch_loop(pool, liveness)
    finally:
        pool.close()
        await stop_health_server(health)
        _remove_pidfile()
        _log.info("[task-maintenance] daemon stopped")


def main() -> None:
    from shared.migrations import assert_schema_current

    assert_schema_current(settings.data_plane.db_url)
    init_gateway_process(name="task_maintenance")
    install_graceful_shutdown("task_maintenance")
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        _log.info("[task-maintenance] interrupted, shutting down")
    except Exception:
        _log.exception("[task-maintenance] daemon crashed — uncaught exception escaped run()")
        raise
    finally:
        _remove_pidfile()


if __name__ == "__main__":
    main()
