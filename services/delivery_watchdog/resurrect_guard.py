"""Escalate repeated delivery auto-resurrect failures into wake suppression."""

from __future__ import annotations

import logging

from psycopg_pool import ConnectionPool

from shared import telemetry
from shared.config import settings
from shared.db_transaction import write_transaction

_log = logging.getLogger("services.delivery_watchdog.resurrect_guard")


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


def record_resurrect_failure(
    pool: ConnectionPool,
    agent_id: int,
    failures_by_agent: dict[int, int],
    suppressions_by_agent: dict[int, int],
) -> None:
    """Count one failure and durably suppress the agent at the threshold."""
    failures = failures_by_agent.get(agent_id, 0) + 1
    failures_by_agent[agent_id] = failures
    threshold = settings.daemon.delivery_watchdog_resurrect_fail_before_suppress
    if failures < threshold:
        _log.debug(
            "[delivery] resurrect retry for terminated agent %s failed (%s/%s)",
            agent_id,
            failures,
            threshold,
        )
        return

    suppress_count = suppressions_by_agent.get(agent_id, 0) + 1
    duration_s = _suppression_duration(suppress_count)
    try:
        written = _write_wake_suppression(pool, agent_id, duration_s)
    except Exception:
        _log.exception("[delivery] failed to suppress automatic wakes for agent %s", agent_id)
        return
    if not written:
        _log.info("[delivery] agent %s disappeared before wake suppression", agent_id)
        return
    failures_by_agent.pop(agent_id, None)
    suppressions_by_agent[agent_id] = suppress_count
    _alert_wake_suppressed(agent_id, failures, duration_s, suppress_count)
