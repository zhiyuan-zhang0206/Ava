"""`insert_event_log_many` — the audit-event batch entry.

Its only caller (`ava.skills._insert_skill_events`) swallows every exception by
design: skill attribution is telemetry and must never take an agent down. That
makes a mistake here invisible in production AND invisible to the skills
tests, which stub the write path at the seam. So the write itself is
exercised against the real emitter: the batch lands in the JSONL mirror (the
durable local copy of the unified stream — the PG `events` table is a
read-only archive since the LGTM cutover, task #1197 close-C) with
category=audit, the same rows the per-row writer produces, in one
round-trip.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import psycopg

from shared import telemetry
from shared.audit_events import insert_event_log, insert_event_log_many


def _agent(conn: psycopg.Connection, aid: int) -> int:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO agents (id) VALUES (%s) ON CONFLICT DO NOTHING", (aid,))
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'test', 'running') "
            "ON CONFLICT DO NOTHING",
            (aid,),
        )
    conn.commit()
    return aid


def _mirror(aid: int) -> list[dict]:
    """Every mirror line for `aid`, oldest first — fail loud on absence."""
    from shared.paths import logs_dir

    telemetry.sync()
    day = datetime.now(UTC).strftime("%Y%m%d")
    path = logs_dir() / f"events-{day}.jsonl"
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if obj.get("agent_id") == aid:
            out.append(obj)
    return out


def _rows(aid: int) -> list[tuple[str, str, dict[str, Any]]]:
    return [(o["event_name"], o["source"], o["attributes"]) for o in _mirror(aid)]


def _event_rows(aid: int) -> list[tuple[str, str, str, dict[str, Any]]]:
    return [(o["event_name"], o["category"], o["source"], o["attributes"]) for o in _mirror(aid)]


def test_batch_writes_one_row_per_payload(db_conn: psycopg.Connection) -> None:
    telemetry.init_telemetry(process="test")
    aid = _agent(db_conn, 8801)
    payloads = [
        {"skill": "alpha", "identifier": "alpha", "invocation_depth": "loaded"},
        {"skill": "beta", "identifier": "grp.beta", "invocation_depth": "loaded"},
    ]
    insert_event_log_many(
        event_type="skill_invoked", agent_id=aid, source="self", payloads=payloads
    )

    rows = _rows(aid)
    assert [p for _t, _s, p in rows] == payloads
    assert {t for t, _s, _p in rows} == {"skill_invoked"}
    assert {s for _t, s, _p in rows} == {"self"}
    # The unified stream carries the same events with category=audit.
    event_rows = _event_rows(aid)
    assert [p for _k, _c, _s, p in event_rows] == payloads
    assert {c for _k, c, _s, _p in event_rows} == {"audit"}


def test_batch_row_matches_the_per_row_writer(db_conn: psycopg.Connection) -> None:
    """The batch and the per-row writer must produce the same rows — the batch
    trades only the never-set `target_agent_id`, nothing else."""
    telemetry.init_telemetry(process="test")
    aid = _agent(db_conn, 8802)
    payload = {"skill": "alpha", "identifier": "alpha", "invocation_depth": "loaded"}
    insert_event_log(event_type="skill_invoked", agent_id=aid, source="self", payload=payload)
    insert_event_log_many(
        event_type="skill_invoked", agent_id=aid, source="self", payloads=[payload]
    )

    single, batched = _rows(aid)
    assert single == batched


def test_empty_batch_writes_nothing(db_conn: psycopg.Connection) -> None:
    telemetry.init_telemetry(process="test")
    aid = _agent(db_conn, 8803)
    insert_event_log_many(event_type="skill_invoked", agent_id=aid, source="self", payloads=[])
    assert _rows(aid) == []
