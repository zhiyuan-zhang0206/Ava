#!/usr/bin/env python3
"""Preview or apply the hosted-inbound dead-letter backlog sweep.

The delivery watchdog now completes two inbound sets that otherwise have no
future consumer: claimed chats older than the configured idling threshold when
their owner is idling, and old pending ``terminate``, ``system_note``, or
``restart_completed`` rows whose owner is terminated. Pending chats are not
included because the G4 resurrect-retry path owns their delivery.

The default mode is read-only and prints both counts, the total row count, and
the backup path an apply would use. Applying requires both ``--apply`` and
``--confirm inbound-sweep-backlog``. Before either UPDATE, apply mode locks and
selects the affected rows and durably writes their id, agent id, kind, status,
claim timestamp, creation timestamp, and content to JSONL. A backup failure
aborts the transaction before any row is changed.

Recovery drill: read each JSONL object and, in one write transaction, UPDATE
``inbound_messages`` by ``id``, setting ``status`` back to that object's
pre-sweep value (``claimed`` or ``pending``) and ``claimed_at`` back to its
saved value. The id-keyed status restoration is the rollback path if these
dead-letter flips ever need to be undone.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from psycopg.rows import class_row

import shared.db
from shared.config import settings
from shared.db_transaction import write_transaction

_CONFIRMATION_TEXT = "inbound-sweep-backlog"
_BACKUP_PREFIX = "inbound_sweep_backlog_backup_"

_CLAIMED_IDLING_SELECT = """
SELECT m.id, m.agent_id, m.kind, m.status, m.claimed_at, m.created_at, m.content
FROM inbound_messages m
JOIN agents_meta am ON am.id = m.agent_id
WHERE m.status = 'claimed'
  AND m.kind = 'chat'
  AND am.status = 'idling'
  AND COALESCE(m.claimed_at, m.created_at)
      < now() - make_interval(secs => %s)
"""

_PENDING_TERMINATED_SELECT = """
SELECT m.id, m.agent_id, m.kind, m.status, m.claimed_at, m.created_at, m.content
FROM inbound_messages m
JOIN agents_meta am ON am.id = m.agent_id
WHERE m.status = 'pending'
  AND m.kind IN ('terminate', 'system_note', 'restart_completed')
  AND am.status = 'terminated'
  AND m.created_at < now() - make_interval(secs => %s)
"""


@dataclass(frozen=True)
class BacklogRow:
    """The complete recovery record required for one affected inbound."""

    id: int
    agent_id: int
    kind: str
    status: str
    claimed_at: datetime | None
    created_at: datetime
    content: str


def _default_backup_path() -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return Path.cwd() / f"{_BACKUP_PREFIX}{timestamp}.jsonl"


def _select_backlog(
    conn: psycopg.Connection,
    *,
    idling_threshold_s: float,
    terminated_threshold_s: float,
    lock_rows: bool,
) -> list[BacklogRow]:
    """Select both sweep sets, optionally locking rows and their owners."""
    lock_clause = " FOR UPDATE OF m, am" if lock_rows else ""
    with conn.cursor(row_factory=class_row(BacklogRow)) as cur:
        cur.execute(
            _CLAIMED_IDLING_SELECT + lock_clause,
            (idling_threshold_s,),
        )
        claimed = cur.fetchall()
        cur.execute(
            _PENDING_TERMINATED_SELECT + lock_clause,
            (terminated_threshold_s,),
        )
        pending = cur.fetchall()
    return sorted([*claimed, *pending], key=lambda row: row.id)


def _write_backup(path: Path, rows: list[BacklogRow]) -> None:
    """Create and fsync one exclusive JSONL backup before database mutation."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            for row in rows:
                handle.write(json.dumps(asdict(row), default=_json_default, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if descriptor != -1:
            os.close(descriptor)


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"cannot serialize backup value of type {type(value).__name__}")


def _apply_backlog(
    backup_path: Path,
    *,
    idling_threshold_s: float,
    terminated_threshold_s: float,
) -> tuple[int, int]:
    """Back up and atomically dead-letter the currently eligible rows."""
    with write_transaction() as conn:
        rows = _select_backlog(
            conn,
            idling_threshold_s=idling_threshold_s,
            terminated_threshold_s=terminated_threshold_s,
            lock_rows=True,
        )
        _write_backup(backup_path, rows)
        claimed_ids = [row.id for row in rows if row.status == "claimed"]
        pending_ids = [row.id for row in rows if row.status == "pending"]

        with conn.cursor() as cur:
            claimed_count = 0
            if claimed_ids:
                cur.execute(
                    "UPDATE inbound_messages m SET status = 'done' "
                    "FROM agents_meta am "
                    "WHERE m.agent_id = am.id "
                    "  AND m.id = ANY(%s) "
                    "  AND m.status = 'claimed' AND m.kind = 'chat' "
                    "  AND am.status = 'idling' "
                    "  AND COALESCE(m.claimed_at, m.created_at) "
                    "      < now() - make_interval(secs => %s)",
                    (claimed_ids, idling_threshold_s),
                )
                claimed_count = cur.rowcount
            pending_count = 0
            if pending_ids:
                cur.execute(
                    "UPDATE inbound_messages m SET status = 'done', claimed_at = now() "
                    "FROM agents_meta am "
                    "WHERE m.agent_id = am.id "
                    "  AND m.id = ANY(%s) "
                    "  AND m.status = 'pending' "
                    "  AND m.kind IN ('terminate', 'system_note', 'restart_completed') "
                    "  AND am.status = 'terminated' "
                    "  AND m.created_at < now() - make_interval(secs => %s)",
                    (pending_ids, terminated_threshold_s),
                )
                pending_count = cur.rowcount
        if (claimed_count, pending_count) != (len(claimed_ids), len(pending_ids)):
            raise RuntimeError(
                "backlog changed after backup selection; database transaction rolled back"
            )
    return claimed_count, pending_count


def _print_plan(rows: list[BacklogRow], backup_path: Path) -> None:
    claimed_count = sum(row.status == "claimed" for row in rows)
    pending_count = sum(row.status == "pending" for row in rows)
    print(f"stale claimed chat row(s) with idling owner: {claimed_count}")
    print(f"stale pending lifecycle row(s) with terminated owner: {pending_count}")
    print(f"row(s) that would be flipped: {len(rows)}")
    print(f"backup path: {backup_path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the backup, then dead-letter both eligible row sets",
    )
    parser.add_argument(
        "--confirm",
        metavar="TEXT",
        help=f"required with --apply; must be exactly {_CONFIRMATION_TEXT!r}",
    )
    parser.add_argument(
        "--backup",
        type=Path,
        help="JSONL backup path (default: timestamped file in the current directory)",
    )
    args = parser.parse_args(argv)
    if args.apply and args.confirm != _CONFIRMATION_TEXT:
        parser.error(f"--apply requires --confirm {_CONFIRMATION_TEXT}")

    idling_threshold = settings.daemon.delivery_watchdog_stale_claimed_idling_threshold_seconds
    terminated_threshold = settings.daemon.delivery_watchdog_stale_claimed_threshold_seconds
    backup_path = args.backup or _default_backup_path()

    if not args.apply:
        with shared.db.connect() as conn:
            rows = _select_backlog(
                conn,
                idling_threshold_s=idling_threshold,
                terminated_threshold_s=terminated_threshold,
                lock_rows=False,
            )
        _print_plan(rows, backup_path)
        print("dry-run — no backup written and no database rows changed")
        return 0

    claimed_count, pending_count = _apply_backlog(
        backup_path,
        idling_threshold_s=idling_threshold,
        terminated_threshold_s=terminated_threshold,
    )
    print(f"backup written: {backup_path}")
    print(f"dead-lettered stale claimed chat row(s) with idling owner: {claimed_count}")
    print(f"dead-lettered stale pending lifecycle row(s) with terminated owner: {pending_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
