# meiva:ref services/derived/insights/run_timeline/docs/range-projection.meiva.okf.md#indexed-history-windows
from __future__ import annotations

from datetime import datetime
from typing import Any

from psycopg import Connection, sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from services.derived.insights.run_timeline.projection.model import VERSION, Projection, Segment
from services.derived.insights.run_timeline.schemas import RunTimelineUnit

_SEGMENT_COLUMNS = """session, checkpoint_id, message_start, message_end, local_start,
    read_before, read_after, start_ts, end_ts, totals"""


def last_segment(conn: Connection[Any], agent_id: int) -> Segment | None:
    with conn.cursor(row_factory=dict_row) as cur:
        row = cur.execute(
            sql.SQL(
                "SELECT {} FROM run_timeline_segments WHERE agent_id = %s AND version = %s ORDER BY session DESC LIMIT 1"
            ).format(sql.SQL(_SEGMENT_COLUMNS)),
            (agent_id, VERSION),
        ).fetchone()
    return Segment.model_validate(row) if row else None


def publish(conn: Connection[Any], agent_id: int, projection: Projection) -> None:
    segment = projection.segment
    data = segment.model_dump()
    data["totals"] = Jsonb(data["totals"])
    conn.execute(
        sql.SQL(
            "INSERT INTO run_timeline_segments (agent_id, version, {}) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        ).format(sql.SQL(_SEGMENT_COLUMNS)),
        (agent_id, VERSION, *data.values()),
    )
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO run_timeline_message_facts"
            " (agent_id, version, session, idx, start_ts, end_ts, request_ts, before_totals, after_totals)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [
                (
                    agent_id,
                    VERSION,
                    segment.session,
                    f.idx,
                    f.start_ts,
                    f.end_ts,
                    f.request_ts,
                    Jsonb(f.before_totals.model_dump()),
                    Jsonb(f.after_totals.model_dump()),
                )
                for f in projection.facts
            ],
        )
        cur.executemany(
            "INSERT INTO run_timeline_blocks"
            " (agent_id, version, session, ordinal, start_ts, end_ts, payload)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            [
                (
                    agent_id,
                    VERSION,
                    segment.session,
                    i,
                    b.start,
                    b.end,
                    b.model_dump_json().encode("utf-8"),
                )
                for i, b in enumerate(projection.blocks)
            ],
        )


def window_blocks(
    conn: Connection[Any],
    agent_id: int,
    sessions: int,
    start: datetime,
    end: datetime,
    *,
    allowance: int | None,
) -> tuple[list[RunTimelineUnit], int]:
    where = (
        "WHERE agent_id = %s AND version = %s AND session < %s AND start_ts <= %s AND end_ts >= %s"
    )
    args = (agent_id, VERSION, sessions, end, start)
    row = conn.execute(
        sql.SQL("SELECT count(*) FROM run_timeline_blocks {}").format(sql.SQL(where)), args
    ).fetchone()
    count = int(row[0]) if row else 0
    if allowance is not None and count > allowance:
        return [], count
    rows = conn.execute(
        sql.SQL("SELECT payload FROM run_timeline_blocks {} ORDER BY session, ordinal").format(
            sql.SQL(where)
        ),
        args,
    ).fetchall()
    return [RunTimelineUnit.model_validate_json(bytes(row[0])) for row in rows], count


def segments_for_span(
    conn: Connection[Any],
    agent_id: int,
    sessions: int,
    start: int,
    end: int,
) -> list[Segment]:
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(
            sql.SQL(
                "SELECT {} FROM run_timeline_segments WHERE agent_id = %s AND version = %s AND session < %s AND message_start <= %s AND message_end > %s ORDER BY session"
            ).format(sql.SQL(_SEGMENT_COLUMNS)),
            (agent_id, VERSION, sessions, end, start),
        ).fetchall()
    return [Segment.model_validate(row) for row in rows]
