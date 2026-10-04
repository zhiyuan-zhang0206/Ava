"""The roster's read model: the last status_probe of each roster-visible agent-runner.

The heartbeat service's liveness pass probes every such machine once a minute and
writes the outcome into `machine_status_snapshot` (`services/wake/heartbeat/liveness.py`).
The gateway's roster and machines reads render from these rows instead of dialing
each runner on every read, so a blackholed host can never cost a read its dial
budget (task #3507) and the gateway keeps no failure memory of its own. A row older
than `MAX_AGE_S` (the heartbeat service is down or wedged), or no row, is not
evidence of anything: the reader dials that machine itself, as an explicit fresh
read would. Splitting reads from dials is the whole point, so the rows carry their
age and the roster renders it (`MachineStatus.observed_at`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from psycopg import Cursor
from psycopg_pool import ConnectionPool

from base.agents.observation.evidence import (
    LIVENESS_PASS_INTERVAL_S,
    MACHINE_OFFLINE_AFTER_FAILURES,
)

# Three liveness-pass intervals: one missed pass plus the pass that rewrites the row.
MAX_AGE_S = 3 * LIVENESS_PASS_INTERVAL_S


@dataclass(frozen=True)
class Snapshot:
    """One machine's last status_probe outcome."""

    observed_at: datetime
    reachable: bool
    consecutive_failures: int
    # The last ClusterStatus payload (kept across a failed attempt); None when the
    # last reachable answer did not validate as ClusterStatus, or none ever arrived.
    status: dict[str, Any] | None
    status_at: datetime | None

    def fresh(self, now: datetime | None = None) -> bool:
        """Whether the heartbeat liveness pass has probed this machine recently."""
        moment = now or datetime.now(UTC)
        return (moment - self.observed_at).total_seconds() <= MAX_AGE_S

    def known_down(self, now: datetime | None = None) -> bool:
        """Whether a recent probe pass failed it often enough to call it offline —
        the same two-consecutive-failures grading the agent liveness uses."""
        return self.fresh(now) and self.consecutive_failures >= MACHINE_OFFLINE_AFTER_FAILURES


def read_all(cur: Cursor[Any]) -> dict[str, Snapshot]:
    """Every machine's snapshot, by machine name."""
    cur.execute(
        "SELECT machine_name, observed_at, reachable, consecutive_failures, status, status_at "
        "FROM machine_status_snapshot"
    )
    return {
        str(name): Snapshot(observed, bool(reachable), int(failures), status, status_at)
        for name, observed, reachable, failures, status, status_at in cur.fetchall()
    }


def read_all_blocking(pool: ConnectionPool[Any]) -> dict[str, Snapshot]:
    """Sync read of every snapshot — via to_thread."""
    with pool.connection() as conn, conn.cursor() as cur:
        return read_all(cur)
