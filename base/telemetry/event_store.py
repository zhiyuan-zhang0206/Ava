"""The durable record of `category=telemetry` and `category=log` events — `telemetry_events`.

Loki keeps an observation copy for Grafana and short windows; this table is the record the
run timeline, the inspector and `/api/events` read. The emitter's drain thread appends every
batch here right after the local JSONL mirror, so the producer never waits and the mirror
stays the fallback: a batch that does not land is still in the mirror, which
`services/events_maintenance/telemetry_replay.py` replays by event id.

Only events somebody reads out of this table are stored: `is_persisted` is the one judgment,
and the live sink, the mirror replay and the backfill script all apply it, so a row the live
path skipped is not brought back by a replay. The mirror and Loki keep every event.

Failure is loud but never raises into the drain thread. The first failure and every 50th after
it log an error through `report_no_pipeline` and emit one `telemetry_store_failed` anomaly
(its counters feed the Prometheus metric an alert can key on). After a failure the writer backs
off, so a down database costs one short attempt per backoff window instead of one per batch.
A process with no database authority (the gate, memory search and other daemons that hold no
login) turns the sink off for good; its events reach the table through the replay.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, LiteralString, cast

import psycopg
from psycopg.types.json import Jsonb

from base.db import Database
from base.events.contract import EVENTS
from base.telemetry.emitter import Event, event_row

STORED_CATEGORIES = frozenset({"telemetry", "log"})
_STORED_LEVELS = frozenset({"warning", "error", "critical"})

# How many months ahead of the current one the writer keeps partitions for.
PARTITION_MONTHS_AHEAD = 3
_STATEMENT_TIMEOUT = "5s"
_LOCK_TIMEOUT = "1s"
_POOL_TIMEOUT_S = 1.0
_BACKOFF_FIRST_S = 2.0
_BACKOFF_MAX_S = 60.0
_REPORT_EVERY = 50

# One JSON text may not carry U+0000: jsonb rejects it. The mirror keeps the original.
_NUL_ESCAPE = re.compile(r"(?<!\\)((?:\\\\)*)\\u0000")

_INSERT: LiteralString = """
INSERT INTO telemetry_events (
    event_uid, ts, trace_id, span_id, agent_id, machine, cluster, process, category,
    event_name, level, source, target_agent_id, attributes, imported_from)
SELECT event_uid, ts, trace_id, span_id, agent_id, machine, cluster, process, category,
       event_name, level, source, target_agent_id, attributes, imported_from
FROM jsonb_to_recordset(%s::jsonb) AS r(
    event_uid bigint, ts timestamptz, trace_id text, span_id text, agent_id bigint,
    machine text, cluster text, process text, category text, event_name text, level text,
    source text, target_agent_id bigint, attributes jsonb, imported_from text)
ON CONFLICT (event_uid, ts) DO NOTHING
"""


def is_persisted(event_name: str, level: str) -> bool:
    """Whether a telemetry or log event belongs in `telemetry_events`.

    Yes for an event whose spec says `persist` (a Postgres reader queries it by name), for any
    event at warning or higher (the resolution counts, the stats and the status page read those
    by level), and for a name the registry does not know. The rest stays in the mirror and Loki.
    """
    spec = EVENTS.get(event_name)
    return spec is None or spec.persist or level.lower() in _STORED_LEVELS


def event_uid(stream_id: int) -> int:
    """The stream id (unsigned blake2b-64) as the signed bigint the table stores."""
    return stream_id - (1 << 64) if stream_id >= 1 << 63 else stream_id


def stored_row(row: dict[str, Any], *, imported_from: str | None = None) -> dict[str, Any]:
    """One mirror/Loki row dict (`event_row` shape, with its `id`) as an insert record."""
    return {
        "event_uid": event_uid(int(row["id"])),
        "ts": row["ts"],
        "trace_id": row["trace_id"],
        "span_id": row["span_id"],
        "agent_id": row["agent_id"],
        "machine": row["machine"],
        "cluster": row["cluster"],
        "process": row["process"],
        "category": row["category"],
        "event_name": row["event_name"],
        "level": row["level"],
        "source": row["source"],
        "target_agent_id": row["target_agent_id"],
        "attributes": row["attributes"],
        "imported_from": imported_from,
    }


def _jsonb(records: Sequence[dict[str, Any]]) -> Jsonb:
    text = json.dumps(list(records), default=str, ensure_ascii=False)
    if "\\u0000" in text:
        text = _NUL_ESCAPE.sub(lambda m: m.group(1) + "\\ufffd", text)
    return Jsonb(json.loads(text))


def insert_rows(conn: psycopg.Connection[Any], records: Sequence[dict[str, Any]]) -> int:
    """Append insert records (`stored_row` shape) in the caller's transaction.

    Idempotent on `(event_uid, ts)`. Returns how many rows were new.
    """
    if not records:
        return 0
    cursor = conn.execute(_INSERT, (_jsonb(records),))
    return max(cursor.rowcount, 0)


def ensure_partitions(conn: psycopg.Connection[Any], *, months_back: int = 1) -> None:
    """Create the partitions from `months_back` months ago through the coming months
    (idempotent, SECURITY DEFINER). A backfill of older rows passes a larger `months_back`
    first: a row outside every partition would land in the default one, and a month cannot be
    created over rows that sit there."""
    conn.execute(
        "SELECT ensure_telemetry_event_partitions(%s, %s)", (PARTITION_MONTHS_AHEAD, months_back)
    )


_lock = threading.Lock()
_pool: Any = None
_disabled = False
_failures = 0
_retry_at = 0.0
_ensured_month: tuple[int, int] | None = None


def _open_pool(db: Database) -> Any:
    global _pool  # noqa: PLW0603
    if _pool is None:
        _pool = db.pool(min_size=0, max_size=1, timeout=_POOL_TIMEOUT_S)
    return _pool


def store_events(db: Database, events: Sequence[Event]) -> None:
    """Emitter sink: append a batch's persisted telemetry and log events to `telemetry_events`.

    Never raises: the drain thread must survive a database that does not answer.
    """
    global _failures  # noqa: PLW0603
    records = [
        stored_row(event_row(event))
        for event in events
        if event.category in STORED_CATEGORIES and is_persisted(event.event_name, event.level)
    ]
    if not records:
        return
    with _lock:
        if _disabled or time.monotonic() < _retry_at:
            return
        try:
            _write(db, records)
        except Exception as exc:
            _failed(exc, len(records))
        else:
            _failures = 0


def _write(db: Database, records: list[dict[str, Any]]) -> None:
    global _ensured_month  # noqa: PLW0603
    from base.db.transaction import write_transaction

    today = datetime.now(UTC)
    month = (today.year, today.month)
    with write_transaction(_open_pool(db), timeout=_POOL_TIMEOUT_S) as conn:
        conn.execute(f"SET LOCAL statement_timeout = '{_STATEMENT_TIMEOUT}'")
        conn.execute(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'")
        if _ensured_month != month:
            ensure_partitions(conn)
        insert_rows(conn, records)
    _ensured_month = month


def _failed(exc: Exception, rows: int) -> None:
    global _disabled, _failures, _retry_at  # noqa: PLW0603
    from base import telemetry
    from base.db.connections import NoDatabaseAuthorityError, PlaceholderDbUrlError
    from base.telemetry import report_no_pipeline

    if isinstance(exc, (NoDatabaseAuthorityError, PlaceholderDbUrlError)):
        _disabled = True
        report_no_pipeline(
            "[telemetry-events] no database authority in this process; its events reach "
            "telemetry_events through the JSONL replay"
        )
        return
    _failures += 1
    _retry_at = time.monotonic() + min(_BACKOFF_FIRST_S * 2 ** (_failures - 1), _BACKOFF_MAX_S)
    if _failures == 1 or _failures % _REPORT_EVERY == 0:
        report_no_pipeline(
            "[telemetry-events] {rows} event(s) did not land in telemetry_events "
            "({n} consecutive failure(s)): {err}; the JSONL mirror holds them for replay",
            rows=rows,
            n=_failures,
            err=repr(exc),
            level="error",
        )
        telemetry.emit(
            "telemetry",
            "telemetry_store_failed",
            level="error",
            attributes={
                "rows": rows,
                "consecutive_failures": _failures,
                "error_class": type(exc).__name__,
                "error": str(exc)[:500],
            },
        )


def close_store() -> None:
    """Close the sink's pool after the emitter has drained its final batch."""
    global _pool  # noqa: PLW0603
    with _lock:
        if _pool is not None:
            _pool.close(timeout=0.5)
            _pool = None


def reset_store() -> None:
    """Return the sink to its initial state, enabled (tests)."""
    global _disabled, _failures, _retry_at, _ensured_month  # noqa: PLW0603
    close_store()
    _disabled = False
    _failures = 0
    _retry_at = 0.0
    _ensured_month = None


def set_enabled(*, enabled: bool) -> bool:
    """Switch the sink on or off; returns the previous state.

    The test session keeps it off (`tests/fixtures/guards.py`) so an event-emitting test does
    not write the shared test database from the drain thread; the tests of this module and of
    its readers turn it on.
    """
    global _disabled  # noqa: PLW0603
    previous = not _disabled
    _disabled = not enabled
    return previous


def jsonl_rows(lines: Sequence[bytes]) -> list[dict[str, Any]]:
    """Insert records for complete mirror lines, ids derived for rows written before they held one."""
    from base.telemetry.emitter import event_id

    records: list[dict[str, Any]] = []
    for raw in lines:
        row = cast("dict[str, Any]", json.loads(raw))
        if row["category"] not in STORED_CATEGORIES or not is_persisted(
            row["event_name"], row["level"]
        ):
            continue
        if "id" not in row:
            stamp = datetime.fromisoformat(row["ts"])
            row["id"] = event_id(raw.decode().rstrip("\r\n"), int(stamp.timestamp() * 1e9))
        records.append(stored_row(row, imported_from="jsonl"))
    return records
