"""PG-backed inspector archive/ledger contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise

import psycopg
import pytest
from psycopg.types.json import Jsonb

from gateway.routers import _inspect_pg, _inspect_stats


def _insert_agent(db: psycopg.Connection) -> int:
    with db.cursor() as cur:
        cur.execute("INSERT INTO agents (label) VALUES ('inspect-pg') RETURNING id")
        row = cur.fetchone()
        assert row is not None
        agent_id = int(row[0])
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'user', 'running')",
            (agent_id,),
        )
    return agent_id


def _insert_event(
    db: psycopg.Connection,
    *,
    agent_id: int,
    event_name: str,
    ts: datetime,
    attributes: dict[str, object],
) -> None:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO events (ts, agent_id, machine, process, category, event_name, level, source, attributes) "
            "VALUES (%s, %s, 'test', 'test', 'telemetry', %s, 'info', 'test', %s::jsonb)",
            (ts, agent_id, event_name, Jsonb(attributes)),
        )


def test_archive_queries_match_loki_filter_and_freeze_contract(
    db_conn: psycopg.Connection,
) -> None:
    agent_id = _insert_agent(db_conn)
    now = datetime.now(tz=UTC)
    _insert_event(
        db_conn,
        agent_id=agent_id,
        event_name="turn_end",
        ts=now - timedelta(minutes=4),
        attributes={"duration_seconds": 4.25, "ok": True},
    )
    _insert_event(
        db_conn,
        agent_id=agent_id,
        event_name="node_exit",
        ts=now - timedelta(minutes=3),
        attributes={"duration_seconds": 3.5, "node": "exec"},
    )
    _insert_event(
        db_conn,
        agent_id=agent_id,
        event_name="node_exit",
        ts=now - timedelta(minutes=2),
        attributes={"duration_seconds": 2.5},
    )
    # The archive cutoff is exclusive: this row establishes the freeze point
    # without being part of any requested event-name pattern.
    _insert_event(
        db_conn,
        agent_id=agent_id,
        event_name="freeze_marker",
        ts=now - timedelta(minutes=1),
        attributes={},
    )
    db_conn.commit()

    assert (
        _inspect_pg.archive_count(
            db_conn,
            agent_id=agent_id,
            event_names=["^turn_end$"],
            categories=["telemetry", "log"],
            attribute_filters={"ok": "true"},
            from_=now - timedelta(hours=1),
            to=None,
        )
        == 1
    )
    assert _inspect_pg.archive_aggregate(
        db_conn,
        field="duration_seconds",
        agg="sum",
        agent_id=agent_id,
        event_names=["^node_exit$"],
        categories=None,
        attribute_filters={"node": "!=claim"},
        from_=now - timedelta(hours=1),
        to=None,
    ) == pytest.approx(6.0)  # pyright: ignore[reportUnknownMemberType]
    assert _inspect_pg.archive_distribution(
        db_conn,
        field="duration_seconds",
        agent_id=agent_id,
        event_names=["^turn_end$"],
        categories=["telemetry", "log"],
        attribute_filters=None,
        from_=now - timedelta(hours=1),
        to=None,
    ) == [(4.25, 1)]


def test_ledger_reads_are_day_scoped_and_return_exclusive_watermarks(
    db_conn: psycopg.Connection,
) -> None:
    agent_id = _insert_agent(db_conn)
    today = datetime.now(tz=UTC).date()
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_metrics_daily "
            "(agent_id, day, turn_total, turn_ok, turn_dur_sum, turn_dur_min, turn_dur_max, exec_ok, exec_failed) "
            "VALUES (%s, %s, 3, 2, 9.5, 1.0, 5.0, 4, 1), (%s, %s, 9, 9, 90, 9, 20, 9, 9)",
            (agent_id, today - timedelta(days=2), agent_id, today - timedelta(days=1)),
        )
        cur.execute(
            "INSERT INTO agent_model_tokens_daily (agent_id, day, model, tokens_out) "
            "VALUES (%s, %s, 'model-a', 40), (%s, %s, 'model-b', 2)",
            (agent_id, today - timedelta(days=2), agent_id, today - timedelta(days=2)),
        )
    db_conn.commit()

    stats = _inspect_pg.ledger_stats(
        db_conn,
        agent_id=agent_id,
        day_from=today - timedelta(days=2),
        day_to=today - timedelta(days=2),
    )
    assert stats == (
        3,
        2,
        9.5,
        1.0,
        5.0,
        4,
        1,
        datetime.combine(today - timedelta(days=1), datetime.min.time(), tzinfo=UTC),
    )
    assert _inspect_pg.ledger_tokens(
        db_conn,
        agent_id=agent_id,
        day_from=today - timedelta(days=2),
        day_to=today - timedelta(days=2),
    ) == (42, datetime.combine(today - timedelta(days=1), datetime.min.time(), tzinfo=UTC))


def test_newest_ledger_day_is_scoped_to_the_requested_range(
    db_conn: psycopg.Connection,
) -> None:
    agent_id = _insert_agent(db_conn)
    today = datetime.now(tz=UTC).date()
    older = today - timedelta(days=2)
    newer = today - timedelta(days=1)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_metrics_daily (agent_id, day, turn_total) "
            "VALUES (%s, %s, 1), (%s, %s, 1)",
            (agent_id, older, agent_id, newer),
        )
    db_conn.commit()

    assert (
        _inspect_pg.newest_ledger_day(db_conn, agent_id=agent_id, day_from=older, day_to=newer)
        == newer
    )
    assert (
        _inspect_pg.newest_ledger_day(db_conn, agent_id=agent_id, day_from=older, day_to=older)
        == older
    )
    assert (
        _inspect_pg.newest_ledger_day(
            db_conn,
            agent_id=agent_id,
            day_from=today - timedelta(days=5),
            day_to=today - timedelta(days=4),
        )
        is None
    )


def test_archive_lifecycle_is_chronological_for_one_replay_pass(
    db_conn: psycopg.Connection,
) -> None:
    agent_id = _insert_agent(db_conn)
    now = datetime.now(tz=UTC)
    _insert_event(
        db_conn,
        agent_id=agent_id,
        event_name="agent_spawned",
        ts=now - timedelta(minutes=4),
        attributes={},
    )
    _insert_event(
        db_conn,
        agent_id=agent_id,
        event_name="agent_terminated",
        ts=now - timedelta(minutes=3),
        attributes={},
    )
    _insert_event(
        db_conn,
        agent_id=agent_id,
        event_name="freeze_marker",
        ts=now - timedelta(minutes=2),
        attributes={},
    )
    db_conn.commit()

    lifecycle = _inspect_pg.archive_lifecycle(
        db_conn,
        agent_id=agent_id,
        from_=now - timedelta(hours=1),
        to=None,
    )
    assert [event_name for _, event_name in lifecycle] == ["agent_spawned", "agent_terminated"]
    assert lifecycle == sorted(lifecycle)


def test_three_hour_shards_and_archive_live_percentile_merging_are_exactly_bounded() -> None:
    start = datetime(2026, 8, 23, 1, 17, tzinfo=UTC)
    end = start + timedelta(hours=9, minutes=2)
    spans = _inspect_pg.split_loki_window(start, end)
    assert spans[0][0] == start
    assert spans[-1][1] == end
    assert all(right == next_left for (_, right), (next_left, _) in pairwise(spans))
    assert all(right - left <= timedelta(hours=3) for left, right in spans)
    assert _inspect_stats.merge_distribution([[(1.9, 2), (2.1, 1)], [(1.1, 3)]]) == [
        (1.0, 5),
        (2.0, 1),
    ]
    assert _inspect_stats.merge_exact_distribution([[(1.9, 2)], [(1.1, 3)]]) == [
        (1.1, 3),
        (1.9, 2),
    ]


def test_full_day_plan_leaves_a_short_window_entirely_on_the_live_edge() -> None:
    start = datetime(2026, 8, 23, 8, tzinfo=UTC)
    end = start + timedelta(hours=6)
    plan = _inspect_stats.full_day_plan(start, end)
    assert not plan.has_full_days
    assert plan.edges == [(start, end)]
    assert plan.day_from is None
    assert plan.day_to is None
