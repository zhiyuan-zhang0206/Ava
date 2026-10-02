"""Replay the local JSONL mirror into `telemetry_events`.

The emitter writes every batch to the mirror before it tries the table, so whatever the table
missed is still in `$AVA_HOME/logs/events-YYYYMMDD.jsonl`: a database that did not answer, or a
process that holds no database login (the gate, memory search, the browser daemons). Each pass
reads the full mirror files from a saved byte position and appends their telemetry and log rows
by event id, so a row the live writer already stored is skipped and a pass can be repeated.

The position is saved with the rows in one transaction, in `agent_metric_file_cursors` under a
`telemetry_events:` key. A partial final line is left for the next pass. Only this machine's
mirror is read; another machine's mirror is replayed by running this on that machine or by
`scripts/data_repair/backfill_telemetry_events.py`.
"""

from __future__ import annotations

import socket
import time
from pathlib import Path
from typing import Any

from psycopg import Connection

from base.paths import logs_dir
from base.telemetry.event_store import ensure_partitions, insert_rows, jsonl_rows

_BATCH_LINES = 500
_PASS_SECONDS = 120.0
_SOURCE_PREFIX = "telemetry_events:"


def replay_file(conn: Connection[Any], path: Path, *, deadline: float) -> int:
    """Append one mirror file's rows from the saved position; return how many were new."""
    source_key = f"{_SOURCE_PREFIX}{socket.gethostname()}:{path.resolve()}"
    stat = path.stat()
    identity = f"{stat.st_dev}:{stat.st_ino}"
    row = conn.execute(
        "SELECT identity, position FROM agent_metric_file_cursors WHERE source_key = %s",
        (source_key,),
    ).fetchone()
    conn.commit()
    position = row[1] if row and row[0] == identity and row[1] <= stat.st_size else 0
    inserted = 0
    with path.open("rb") as source:
        source.seek(position)
        while source.tell() < stat.st_size and time.monotonic() < deadline:
            lines: list[bytes] = []
            for _ in range(_BATCH_LINES):
                before = source.tell()
                line = source.readline()
                if not line or not line.endswith(b"\n"):
                    source.seek(before)  # a line still being appended
                    break
                lines.append(line)
            if not lines:
                break
            with conn.transaction():
                ensure_partitions(conn)
                inserted += insert_rows(conn, jsonl_rows(lines))
                conn.execute(
                    "INSERT INTO agent_metric_file_cursors (source_key, identity, position) "
                    "VALUES (%s, %s, %s) ON CONFLICT (source_key) DO UPDATE SET "
                    "identity = EXCLUDED.identity, position = EXCLUDED.position",
                    (source_key, identity, source.tell()),
                )
    return inserted


def recover_telemetry_events(conn: Connection[Any]) -> int:
    """One bounded pass over this machine's full mirror files, newest first."""
    deadline = time.monotonic() + _PASS_SECONDS
    inserted = 0
    for path in sorted(logs_dir().glob("events-????????.jsonl"), reverse=True):
        if time.monotonic() >= deadline:
            break
        try:
            inserted += replay_file(conn, path, deadline=deadline)
        except Exception:
            conn.rollback()
            raise
    return inserted
