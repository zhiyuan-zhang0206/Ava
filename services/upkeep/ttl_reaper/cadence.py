"""Durable cadence clocks for the reaper's slow phases.

The fire-log prune (daily), the torn lifecycle-pointer scan and the
absent-machine fence settle (hourly) run once per interval, not once per pass.
The clock lives in `maintenance_state`, one row per phase, so a restart resumes
the cadence instead of running every slow phase at once.

Synchronous psycopg; the loops call it through `asyncio.to_thread`.
"""

from __future__ import annotations

from psycopg_pool import ConnectionPool

from base.db.transaction import write_transaction

FIRE_LOG_PRUNE = "schedule_fire_log_prune"
TORN_POINTER_SCAN = "torn_pointer_scan"
ABSENT_FENCE_SETTLE = "absent_fence_settle"

HOURLY_S = 3600.0


def claim_due(pool: ConnectionPool, kind: str, interval_s: float) -> bool:
    """Claim phase `kind` when `interval_s` has elapsed since its last claim.

    One statement checks the clock and stamps it, so the phase is handed out
    once per interval even across restarts. The stamp is the claim, not the
    outcome: a phase that fails waits out the interval like one that succeeded,
    so a persistent failure cannot spin.
    """
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO maintenance_state (kind) VALUES (%s) "
            "ON CONFLICT (kind) DO UPDATE SET last_run_at = clock_timestamp() "
            "WHERE maintenance_state.last_run_at "
            "      < clock_timestamp() - make_interval(secs => %s) "
            "RETURNING kind",
            (kind, interval_s),
        )
        return cur.fetchone() is not None
