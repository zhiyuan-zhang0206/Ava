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
  `remind_interval_seconds` window and deliver one system-note digest per owner. An
  overdue window repeats at max(backoff, remind_interval_seconds), so a P3 task
  (4h interval) is not nagged hourly; `last_reminded_at` gates it. Digest and
  counter acceptance share a transaction under the selected task row locks;
  a rollback leaves both absent, and a lost post-commit hint cannot duplicate it.
- Escalate: when `reminder_count` reaches `AVA_TASK_ESCALATE_N` (default 3),
  notify the parent task's owner (the delegator) that the current owner is
  unresponsive — once per overdue window: the delivered digest stamps
  `escalated_at`, and any update() clears it with the reminder counters. A
  top-level task has no delegating parent owner (its parent is
  the ownerless system root), so it escalates to the user instead
  — a require_response notice posted on the stalled owner that surfaces in the
  human queue, grouped under the task.

No stale sweep, no automatic cancellation, no orphan release. The system only
speaks — posting a notice or a message; it never changes task state.

Usage:
    .venv/bin/python -m ava_builtins.plugins.ava_fleet.task_maintenance.daemon

Kept alive by the root supervisor's health monitor through the `/healthz`
identity probe of the plugin's `services()` entry — so the schema-drift exit in
`_dispatch_loop` is revived on the next round instead of staying dead.
"""

import asyncio
import logging
import os
import sys
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path

import psycopg
from psycopg_pool import ConnectionPool

from ava_builtins.plugins.ava_fleet.default_config import FleetConfig
from base import telemetry
from base.cluster.machine import validate_machine_name
from base.config import settings
from base.daemon.endpoints import ServiceEndpoint, ServiceEndpoints
from base.daemon.health import (
    Liveness,
    start_health_server,
    stop_health_server,
)
from base.daemon.shutdown import install_graceful_shutdown
from base.db import Database, insert_inbound_message_in_transaction, publish_inbound_wake
from base.db.code_version_gate import ProcessDbGate
from base.db.transaction import write_transaction
from base.events.live.announce import publish_agent_updated_sync
from base.events.live.bus import EventBus
from base.log import init_gateway_process
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from base.packages.plugins.config_registration import (
    disk_image_path,
    read_authority_config,
    read_service_config,
)
from services.pidfile import acquire_pidfile, pidfile_holds_daemon, remove_pidfile

_log = logging.getLogger("ava_builtins.plugins.ava_fleet.task_maintenance.daemon")


def _endpoint() -> ServiceEndpoint:
    return ServiceEndpoints.from_settings().of("task_maintenance")


def _pidfile() -> Path:
    return _endpoint().pidfile


_LIVENESS_TIMEOUT_S = 60.0
_LIVENESS_BEAT_STEP_S = 30.0


# ── Reminder pass ──────────────────────────────────────────────────────────────

# Tasks whose remind_interval_seconds has elapsed AND the owner hasn't been reminded
# within this overdue window (last_reminded_at gates it within
# max(backoff, remind_interval_seconds) — each task repeats at its own cadence).
# Terminated owners remain eligible: direct delivery records without reviving them.
_REMINDER_SQL = """
    SELECT id, owner, title, remind_interval_seconds,
           EXTRACT(EPOCH FROM (now() - updated_at))::bigint AS elapsed,
           priority
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
    ORDER BY priority, id
"""


def _advance_reminder_counters(cur: psycopg.Cursor, task_id: int) -> None:
    """Advance cadence in the transaction containing the reminder inbound."""
    cur.execute(
        "UPDATE agent_tasks SET last_reminded_at = now(), reminder_count = reminder_count + 1 "
        "WHERE id = %s",
        (task_id,),
    )


def _reminder_digest_message(tasks: list[tuple[int, str, int, int, str]]) -> str:
    """Format one owner's overdue tasks without a single-task special case.

    The copy is imperative (user ruling 2026-08-29): each line orders the
    owner to report current status and push the next step, and to state why
    and raise the reminder interval when it truly cannot advance -- an
    explicit wait instead of silent idling. Tasks arrive priority-sorted
    (P0 first) from _REMINDER_SQL; the idle window and reminder interval
    stay on the line so the owner sees the silence it is being asked to
    break."""
    lines = [
        f"Task reminders — you have {len(tasks)} overdue task(s). "
        "Report status and push each forward now:"
    ]
    for task_id, title, remind_interval_seconds, elapsed, _priority in tasks:
        lines.append(
            f'- #{task_id} "{title}" — idle {elapsed / 3600:.1f}h '
            f"(reminder interval: {remind_interval_seconds // 60}min): report your "
            "current status and advance the next step; if you cannot advance, state "
            "why and raise the reminder interval to wait explicitly"
        )
    return "\n".join(lines)


def _run_reminders(
    pool: ConnectionPool,
    db: Database,
    bus: EventBus,
    backoff_seconds: float,
) -> int:
    """Commit owner digests and every task's reminder cadence together."""
    queued: list[tuple[int, int, str, list[int]]] = []
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(_REMINDER_SQL + " FOR UPDATE SKIP LOCKED", (backoff_seconds,))
        overdue_by_owner: dict[int, list[tuple[int, str, int, int, str]]] = defaultdict(list)
        for task_id, owner, title, interval, elapsed, priority in cur.fetchall():
            overdue_by_owner[owner].append((task_id, title, interval, elapsed, priority))
        for owner, tasks in overdue_by_owner.items():
            content = _reminder_digest_message(tasks)
            inbound_id, _ = insert_inbound_message_in_transaction(
                cur,
                owner,
                content,
                "system",
                kind="system_note",
                payload={
                    "note_tag": "task",
                    "task_notification": True,
                    "delivery_resurrect": False,
                },
            )
            task_ids = [task_id for task_id, *_ in tasks]
            for task_id in task_ids:
                _advance_reminder_counters(cur, task_id)
            queued.append((owner, inbound_id, content, task_ids))
    for owner, inbound_id, content, task_ids in queued:
        announce_reminder(db, bus, owner, inbound_id, content)
        telemetry.emit(
            "telemetry",
            "task_reminder_digest",
            agent_id=owner,
            source="system",
            attributes={"owner_id": owner, "task_count": len(task_ids), "task_ids": task_ids},
        )
    return len(queued)


def announce_reminder(
    db: Database, bus: EventBus, owner: int, inbound_id: int, content: str
) -> None:
    """Best-effort hints after durable acceptance; watchdog heals a lost wake."""
    del content
    try:
        publish_agent_updated_sync(bus, owner)
        publish_inbound_wake(db, bus, owner, str(inbound_id))
    except Exception:
        _log.exception("[task-maintenance] committed reminder %s lost its live hint", inbound_id)


# ── Escalate pass ────────────────────────────────────────────────────────────


def _run_escalate(pool: ConnectionPool, db: Database, bus: EventBus, escalate_n: int) -> int:
    """Accept current escalation state atomically, then announce committed work."""
    from ava_builtins.plugins.ava_fleet.task_maintenance.escalation import accept_escalations

    receipts = accept_escalations(pool, escalate_n)
    for receipt in receipts:
        if receipt.inbound_id is not None:
            announce_reminder(db, bus, receipt.recipient, receipt.inbound_id, receipt.content)
        else:
            publish_agent_updated_sync(bus, receipt.recipient)
        telemetry.emit(
            "telemetry",
            "task_escalation",
            agent_id=receipt.recipient,
            source="system",
            attributes={
                "owner_id": receipt.recipient,
                "task_count": len(receipt.task_ids),
                "task_ids": receipt.task_ids,
                "leg": receipt.leg,
            },
        )
    if receipts:
        _log.info("[task-maintenance] accepted %d escalation digests/notices", len(receipts))
    return len(receipts)


# ── Daemon lifecycle ─────────────────────────────────────────────────────────


def _write_pidfile() -> None:
    if not acquire_pidfile(_pidfile(), "ava_builtins.plugins.ava_fleet.task_maintenance.daemon"):
        _log.info("[task_maintenance] daemon already running (pidfile=%s), exiting", _pidfile())
        sys.exit(1)


def _remove_pidfile() -> None:
    remove_pidfile(_pidfile())


def _is_running() -> bool:
    """Whether a daemon is already running (via its pidfile).

    Pid-reuse-safe: a live pid whose argv does not name this daemon's module
    is a recycled pid, not a running instance (audit round 2, P1)."""
    return pidfile_holds_daemon(
        _pidfile(), "ava_builtins.plugins.ava_fleet.task_maintenance.daemon"
    )


async def _sleep_with_liveness(liveness: Liveness, total_s: float) -> None:
    remaining = total_s
    while remaining > 0:
        liveness.beat()
        step = min(_LIVENESS_BEAT_STEP_S, remaining)
        await asyncio.sleep(step)
        remaining -= step


async def _dispatch_loop(
    pool: ConnectionPool,
    db: Database,
    bus: EventBus,
    liveness: Liveness,
    *,
    config: FleetConfig,
) -> None:
    interval = config.task_maintenance_interval_seconds
    backoff_seconds = config.task_reminder_backoff_seconds
    escalate_n = config.task_escalate_n
    _log.info(
        "[task-maintenance] daemon started, pid=%s, interval=%.0fs, backoff=%.0fs, escalate_n=%d",
        os.getpid(),
        interval,
        backoff_seconds,
        escalate_n,
    )
    while True:
        try:
            _run_reminders(pool, db, bus, backoff_seconds)
            _run_escalate(pool, db, bus, escalate_n)
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
        # discipline as services/upkeep/events_maintenance/daemon.py — audit
        # round 2, P1: the sleep used to sit inside the try, so a
        # non-ProgrammingError exception skipped it and the loop spun).
        await _sleep_with_liveness(liveness, interval)


async def run(
    config: FleetConfig, *, database: Callable[[], Database], image: LoadedCommit
) -> None:
    if _is_running():
        _log.info(
            "[task-maintenance] daemon already running (pidfile=%s), exiting",
            _pidfile(),
        )
        sys.exit(1)

    # Pidfile before the healthz bind — see services/restarter/daemon.py:run().
    _write_pidfile()
    _log.info("[task-maintenance] pidfile written: %s", _pidfile())

    liveness = Liveness(_LIVENESS_TIMEOUT_S)
    endpoint = _endpoint()
    health = await start_health_server(
        "task_maintenance", endpoint.health_port, liveness=liveness, image=image
    )
    _log.info("[task-maintenance] healthz listening on :%s", endpoint.health_port)

    db = database()
    pool = db.pool()
    try:
        await _dispatch_loop(pool, db, EventBus.from_settings(), liveness, config=config)
    finally:
        pool.close()
        await stop_health_server(health)
        _remove_pidfile()
        _log.info("[task-maintenance] daemon stopped")


def main() -> None:
    from base.deploy.schema.migrations import assert_schema_current

    image = LoadedCommit.capture()
    assert_schema_current(settings.data_plane.db_url)
    version = CodeVersion(image)
    gate = ProcessDbGate(version=version.get, process="task_maintenance", exempt=False)

    def database() -> Database:
        return Database.from_settings(gate=gate)

    pipeline = telemetry.build_pipeline(database=database)
    init_gateway_process(
        name="task_maintenance",
        producer=lambda: pipeline,
        machine_reader=lambda: validate_machine_name(settings.general.machine_name),
        image=image,
    )
    install_graceful_shutdown("task_maintenance")
    try:
        config = read_service_config("ava_fleet", FleetConfig)
        if config is None:
            config = read_authority_config("ava_fleet", FleetConfig, disk_image_path("ava_fleet"))
        asyncio.run(run(config, database=database, image=image))
    except KeyboardInterrupt:
        _log.info("[task-maintenance] interrupted, shutting down")
    except Exception:
        _log.exception("[task-maintenance] daemon crashed — uncaught exception escaped run()")
        raise
    finally:
        _remove_pidfile()


if __name__ == "__main__":
    main()
