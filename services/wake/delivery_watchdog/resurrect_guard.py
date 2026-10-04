"""Escalate repeated delivery auto-resurrect failures into wake suppression."""

from __future__ import annotations

import logging

from psycopg_pool import ConnectionPool

from base import telemetry
from base.config import settings
from base.db.transaction import write_transaction
from services.wake.delivery_watchdog import attempts

_log = logging.getLogger("services.wake.delivery_watchdog.resurrect_guard")


def _suppression_duration(suppress_count: int) -> float:
    duration = settings.daemon.delivery_watchdog_suppress_base_seconds
    maximum = settings.daemon.delivery_watchdog_suppress_max_seconds
    for _ in range(suppress_count - 1):
        duration = min(duration * 2, maximum)
        if duration >= maximum:
            break
    return min(duration, maximum)


def _write_wake_suppression(pool: ConnectionPool, agent_id: int, duration_s: float) -> bool:
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta "
            "SET wake_suppressed_until=clock_timestamp()+make_interval(secs => %s), "
            "    wake_suppress_reason='resurrect_failed' "
            "WHERE id=%s RETURNING id",
            (duration_s, agent_id),
        )
        return cur.fetchone() is not None


def _alert_wake_suppressed(
    agent_id: int,
    consecutive_failures: int,
    suppress_seconds: float,
    suppress_count: int,
) -> None:
    _log.warning(
        "[delivery] suppressed automatic wakes for agent %s for %.0fs after %s "
        "consecutive resurrect failures (suppression %s)",
        agent_id,
        suppress_seconds,
        consecutive_failures,
        suppress_count,
    )
    try:
        telemetry.emit(
            "telemetry",
            "delivery_wake_suppressed",
            level="warning",
            agent_id=agent_id,
            source="system",
            attributes={
                "consecutive_failures": consecutive_failures,
                "suppress_seconds": suppress_seconds,
                "suppress_count": suppress_count,
                "reason": "resurrect_failed",
            },
        )
    except Exception:
        _log.exception("[delivery] delivery_wake_suppressed emit failed for agent %s", agent_id)


def record_resurrect_failure(pool: ConnectionPool, agent_id: int) -> None:
    """Count one failure and durably suppress the agent at the threshold.

    The counters live in `delivery_watchdog_attempts` (the loop's claim created
    the row), so the escalation ladder survives a watchdog restart."""
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE delivery_watchdog_attempts SET consecutive_failures = consecutive_failures + 1 "
            "WHERE kind = %s AND agent_id = %s RETURNING consecutive_failures, suppress_count",
            (attempts.RESURRECT, agent_id),
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError(f"resurrect failure recorded for agent {agent_id} without a claim")
    failures, previous_suppressions = row
    threshold = settings.daemon.delivery_watchdog_resurrect_fail_before_suppress
    if failures < threshold:
        _log.debug(
            "[delivery] resurrect retry for terminated agent %s failed (%s/%s)",
            agent_id,
            failures,
            threshold,
        )
        return

    suppress_count = previous_suppressions + 1
    duration_s = _suppression_duration(suppress_count)
    try:
        written = _write_wake_suppression(pool, agent_id, duration_s)
    except Exception:
        _log.exception("[delivery] failed to suppress automatic wakes for agent %s", agent_id)
        return
    if not written:
        _log.info("[delivery] agent %s disappeared before wake suppression", agent_id)
        return
    with write_transaction(pool) as conn:
        conn.execute(
            "UPDATE delivery_watchdog_attempts SET consecutive_failures = 0, suppress_count = %s "
            "WHERE kind = %s AND agent_id = %s",
            (suppress_count, attempts.RESURRECT, agent_id),
        )
    _alert_wake_suppressed(agent_id, failures, duration_s, suppress_count)


def record_resurrect_success(pool: ConnectionPool, agent_id: int) -> None:
    """A resurrect that left the owner terminated no more: both escalation
    counters start over."""
    with write_transaction(pool) as conn:
        conn.execute(
            "UPDATE delivery_watchdog_attempts SET consecutive_failures = 0, suppress_count = 0 "
            "WHERE kind = %s AND agent_id = %s",
            (attempts.RESURRECT, agent_id),
        )
