"""Retention policies for the append-only fact tables beside the `events`
stream — the daemon's table-retention slice.

`events` has a full lifecycle (partition + per-event retention, see
`services.events_maintenance.retention`); the fact tables below had none — rows
accumulated forever (audit round 2, perf report: 8 tables with zero retention).
This module is the policy registry: one entry per table, declaring which
terminal-state rows may be deleted and after how long. Conservative by design:

- Only terminal rows are ever deleted — open / live rows (pending inbounds,
  open notices, open pages, open/in_progress tasks) are never touched.
- Tables with no policy are never touched.
- `agents` / `agents_meta` deliberately have NO policy: agent rows are the
  fleet's audit identity (the events stream keeps `agent_id` references for
  the whole retention window, the spawn tree and task ownership FK to them),
  and their row size is trivial next to the checkpoint/event storage. They are
  permanent by design; revisit only with an archival feature.
- `agent_activity` is frozen (zero writers since the SDK verb was removed
  2026-08-02); its historical endpoint keeps the table, it does not grow, and
  its fate is the 8/12 legacy-table cleanup, not a retention policy here.
- `delivery_watchdog_alerted` rows cascade away with their inbound message.

Each pass runs one DELETE per table on the maintenance daemon's hourly loop
(the events-maintenance daemon), reports deleted counts, and is idempotent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import LiteralString

import psycopg
from psycopg import sql

# Age column and per-table minimum age (days) — the registry.
# `delete_where` must be a static SQL predicate naming only terminal states
# (never interpolated with request data).


@dataclass(frozen=True)
class TablePolicy:
    """One table's retention rule: delete rows matching `delete_where` whose
    `age_column` is older than `min_age_days`."""

    table: str
    # LiteralString: sql.SQL() demands a static literal (no runtime
    # interpolation); the registry values are all literals, so the type
    # enforces the no-dynamic-SQL rule at compile time.
    delete_where: LiteralString
    min_age_days: int
    age_column: str = "created_at"


TABLE_POLICIES: tuple[TablePolicy, ...] = (
    # Delivered chat/lifecycle inbounds: content value decays fast; the agent's
    # own history lives in checkpoints + events, not here. 90d matches the
    # telemetry floor.
    TablePolicy("inbound_messages", "status = 'done'", 90),
    # Resolved notices (answered/dismissed/read/withdrawn/superseded): the
    # decision record is audit-like, so align with the audit retention.
    TablePolicy("agent_notices", "resolved_at IS NOT NULL", 365, age_column="resolved_at"),
    # Closed/expired page rows: the row is a registry entry, no value after
    # terminalization. Two policies because the generic machinery ages ONE
    # column and `NULL < x` is never true — a single policy with either column
    # would silently never delete the other kind of terminal row.
    TablePolicy("agent_pages", "closed_at IS NOT NULL", 90, age_column="closed_at"),
    TablePolicy(
        "agent_pages",
        "expired_at IS NOT NULL AND closed_at IS NULL",
        90,
        age_column="expired_at",
    ),
    # Done/cancelled tasks: the audit trail lives in the events stream
    # (task_update events), so old terminal rows are safe to drop. Guards keep
    # rows referenced as a parent task or by a notice (FKs without cascade).
    TablePolicy(
        "agent_tasks",
        "status IN ('done', 'cancelled')"
        " AND id NOT IN (SELECT parent_id FROM agent_tasks WHERE parent_id IS NOT NULL)"
        " AND id NOT IN (SELECT task_id FROM agent_notices WHERE task_id IS NOT NULL)",
        365,
        age_column="updated_at",
    ),
    # Ops-monitor buckets: the ops panel window is 7d; 90d keeps Grafana
    # lookbacks without unbounded growth (~2.3k rows/day at 60s buckets).
)


@dataclass(frozen=True)
class TableRetentionResult:
    """What one pass deleted, per table (only non-empty deletes)."""

    deleted: tuple[tuple[str, int], ...]

    def summary(self) -> str:
        """One-line human summary for the daemon log; empty when nothing was
        deleted this pass."""
        return "; ".join(f"{table}: {rows} rows" for table, rows in self.deleted)


def apply_table_retention(conn: psycopg.Connection, *, now_utc: datetime) -> TableRetentionResult:
    """One pass over the policy registry: for each table, delete the terminal
    rows past their retention. One DELETE per table, committed by the caller's
    connection mode (the daemon runs this on a fresh pool connection, so each
    table's delete commits independently). Idempotent — re-running after a
    successful pass deletes nothing."""
    deleted: list[tuple[str, int]] = []
    for policy in TABLE_POLICIES:
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("DELETE FROM {} WHERE {} AND {} < %s").format(
                    sql.Identifier(policy.table),
                    sql.SQL(policy.delete_where),
                    sql.Identifier(policy.age_column),
                ),
                (now_utc - timedelta(days=policy.min_age_days),),
            )
            if cur.rowcount:
                deleted.append((policy.table, cur.rowcount))
    return TableRetentionResult(deleted=tuple(deleted))
