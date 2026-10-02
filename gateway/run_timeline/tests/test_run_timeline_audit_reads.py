"""The run timeline reads audit facts (compact, spawn, terminate, ...) from `audit_events`.

Loki's projection of them expires, so the timeline would lose every older marker; the audit
names come from Postgres and only the rest from Loki.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from gateway.run_timeline import _events as reads


def _record(db: psycopg.Connection, agent_id: int, name: str, *, hours_ago: float) -> None:
    db.execute(
        "INSERT INTO audit_events (event_uid, ts, machine, process, event_name, level, source, "
        "agent_id) VALUES (%s, now() - (%s * interval '1 hour'), 'm', 'p', %s, 'info', 'test', %s)",
        (uuid.uuid4().int % (1 << 62), hours_ago, name, agent_id),
    )
    db.commit()


def test_audit_names_come_from_postgres_and_the_rest_from_loki(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record(db_conn, 405, "compact", hours_ago=200)
    _record(db_conn, 405, "spawn", hours_ago=300)
    _record(db_conn, 406, "compact", hours_ago=200)
    asked: list[object] = []

    def query(**kwargs: object) -> tuple[list[dict[str, object]], bool]:
        asked.append(kwargs["event_names"])
        return [{"id": 1, "event_name": "turn_end"}], False

    monkeypatch.setattr(reads.loki_events, "query_events", query)
    now = datetime.now(UTC)

    events = reads.query_all_events(
        405, now - timedelta(days=30), now, event_names=("compact", "spawn", "turn_end")
    )

    assert sorted(str(event["event_name"]) for event in events) == ["compact", "spawn", "turn_end"]
    assert asked == [["turn_end"]]


def test_only_audit_names_never_touch_loki(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record(db_conn, 405, "terminate", hours_ago=1)

    def query(**_kwargs: object) -> tuple[list[dict[str, object]], bool]:
        raise AssertionError("an audit-only read must not query Loki")

    monkeypatch.setattr(reads.loki_events, "query_events", query)
    now = datetime.now(UTC)

    events = reads.query_all_events(405, now - timedelta(days=1), now, event_names=("terminate",))

    assert [event["event_name"] for event in events] == ["terminate"]
