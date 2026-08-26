"""PG-backed inspector archive/ledger contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any, Never

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from gateway import events_archive
from gateway.routers import _inspect_pg, _inspect_stats
from shared.config import settings


class _BoundaryCursor:
    def __init__(self, boundary: datetime | None) -> None:
        self.boundary = boundary
        self.executions = 0

    def execute(self, query: str) -> None:
        assert query == "SELECT max(ts) FROM events"
        self.executions += 1

    def fetchone(self) -> tuple[datetime | None] | None:
        return (self.boundary,)


def test_frozen_archive_boundary_is_loaded_once_per_process() -> None:
    boundary = datetime(2026, 8, 13, tzinfo=UTC)
    first_cursor = _BoundaryCursor(boundary)
    second_cursor = _BoundaryCursor(None)

    assert events_archive.frozen_boundary() is None
    assert events_archive.load_frozen_boundary(first_cursor) == boundary
    assert events_archive.load_frozen_boundary(second_cursor) == boundary
    assert first_cursor.executions == 1
    assert second_cursor.executions == 0


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


def test_archive_node_exit_seconds_reads_legacy_and_aggregated_rows(
    db_conn: psycopg.Connection,
) -> None:
    """The archive reader must not NULL out on either retained node_exit shape:
    legacy per-node rows (top-level node/duration_seconds) and aggregated
    per-turn rows (attributes.nodes list, shape since PR #654) — review #654-2."""
    agent_id = _insert_agent(db_conn)
    now = datetime.now(tz=UTC)
    # Legacy per-node rows (pre-aggregation shape).
    _insert_event(
        db_conn,
        agent_id=agent_id,
        event_name="node_exit",
        ts=now - timedelta(minutes=4),
        attributes={"node": "llm", "duration_seconds": 10.0, "outcome": "ok"},
    )
    _insert_event(
        db_conn,
        agent_id=agent_id,
        event_name="node_exit",
        ts=now - timedelta(minutes=4),
        attributes={"node": "exec", "duration_seconds": 20.0, "outcome": "ok"},
    )
    _insert_event(
        db_conn,
        agent_id=agent_id,
        event_name="node_exit",
        ts=now - timedelta(minutes=4),
        attributes={"node": "claim", "duration_seconds": 3000.0, "outcome": "ok"},
    )
    # Aggregated per-turn row (one event, per-node entries inside `nodes`).
    _insert_event(
        db_conn,
        agent_id=agent_id,
        event_name="node_exit",
        ts=now - timedelta(minutes=2),
        attributes={
            "count": 3,
            "nodes": [
                {"node": "llm", "outcome": "ok", "duration_seconds": 5.0},
                {"node": "exec", "outcome": "ok", "duration_seconds": 6.0},
                {"node": "claim", "outcome": "ok", "duration_seconds": 1.0},
            ],
        },
    )
    # Exclusive freeze boundary: this row establishes the cutoff without
    # participating in the sums.
    _insert_event(
        db_conn,
        agent_id=agent_id,
        event_name="freeze_marker",
        ts=now - timedelta(minutes=1),
        attributes={},
    )
    db_conn.commit()

    assert _inspect_pg.archive_node_exit_seconds(
        db_conn, agent_id=agent_id, node="exclude_claim", from_=None, to=None
    ) == pytest.approx(10.0 + 20.0 + 5.0 + 6.0)  # pyright: ignore[reportUnknownMemberType]
    # claim rows excluded on both shapes
    assert _inspect_pg.archive_node_exit_seconds(
        db_conn, agent_id=agent_id, node="exec", from_=None, to=None
    ) == pytest.approx(20.0 + 6.0)  # pyright: ignore[reportUnknownMemberType]
    # Windowed read behaves the same (from_/to bound both shapes).
    assert _inspect_pg.archive_node_exit_seconds(
        db_conn,
        agent_id=agent_id,
        node="exclude_claim",
        from_=now - timedelta(minutes=3),
        to=None,
    ) == pytest.approx(5.0 + 6.0)  # pyright: ignore[reportUnknownMemberType]


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


def test_ledger_distribution_ignores_zero_turn_days_and_requires_histograms_for_turns(
    db_conn: psycopg.Connection,
) -> None:
    agent_id = _insert_agent(db_conn)
    today = datetime.now(tz=UTC).date()
    first_day = today - timedelta(days=4)
    final_day = today - timedelta(days=2)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_metrics_daily (agent_id, day, turn_total, turn_dur_hist) "
            "VALUES (%s, %s, %s, %s), (%s, %s, %s, %s)",
            (
                agent_id,
                first_day,
                3,
                Jsonb({"1": 2, "4": 1}),
                agent_id,
                final_day,
                5,
                Jsonb({"1": 3, "9": 2}),
            ),
        )
    db_conn.commit()

    # The absent middle day is an archive-era ledger gap, not an incomplete
    # histogram: only actual ledger rows take part in the coverage check.
    assert _inspect_pg.ledger_distribution(
        db_conn, agent_id=agent_id, day_from=first_day, day_to=final_day
    ) == ([(1.0, 5), (4.0, 1), (9.0, 2)], True)

    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_metrics_daily (agent_id, day, turn_total) VALUES (%s, %s, 0)",
            (agent_id, today - timedelta(days=1)),
        )
    db_conn.commit()

    distribution, complete = _inspect_pg.ledger_distribution(
        db_conn, agent_id=agent_id, day_from=first_day, day_to=today - timedelta(days=1)
    )
    assert distribution == [(1.0, 5), (4.0, 1), (9.0, 2)]
    assert complete is True

    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_metrics_daily (agent_id, day, turn_total) VALUES (%s, %s, 1)",
            (agent_id, today),
        )
    db_conn.commit()

    _, complete = _inspect_pg.ledger_distribution(
        db_conn, agent_id=agent_id, day_from=first_day, day_to=today
    )
    assert complete is False


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


def test_archive_stats_rollup_matches_raw_reads(db_conn: psycopg.Connection) -> None:
    agent_id = _insert_agent(db_conn)
    no_archive_agent_id = _insert_agent(db_conn)
    now = datetime.now(tz=UTC)
    _insert_event(
        db_conn,
        agent_id=agent_id,
        event_name="turn_end",
        ts=now - timedelta(minutes=6),
        attributes={"duration_seconds": 1.5},
    )
    _insert_event(
        db_conn,
        agent_id=agent_id,
        event_name="turn_end",
        ts=now - timedelta(minutes=5),
        attributes={"duration_seconds": 2.5},
    )
    _insert_event(
        db_conn,
        agent_id=agent_id,
        event_name="turn_end",
        ts=now - timedelta(minutes=4),
        attributes={"duration_seconds": 2.5},
    )
    _insert_event(
        db_conn,
        agent_id=agent_id,
        event_name="node_exit",
        ts=now - timedelta(minutes=3),
        attributes={"duration_seconds": 3.0, "node": "claim"},
    )
    _insert_event(
        db_conn,
        agent_id=agent_id,
        event_name="node_exit",
        ts=now - timedelta(minutes=2),
        attributes={"duration_seconds": 5.0, "node": "exec"},
    )
    _insert_event(
        db_conn,
        agent_id=agent_id,
        event_name="node_exit",
        ts=now - timedelta(minutes=1, seconds=30),
        attributes={"duration_seconds": 7.0, "node": "other"},
    )
    _insert_event(
        db_conn,
        agent_id=agent_id,
        event_name="agent_spawned",
        ts=now - timedelta(minutes=1, seconds=15),
        attributes={},
    )
    _insert_event(
        db_conn,
        agent_id=agent_id,
        event_name="agent_terminated",
        ts=now - timedelta(minutes=1),
        attributes={},
    )
    _insert_event(
        db_conn,
        agent_id=agent_id,
        event_name="freeze_marker",
        ts=now - timedelta(seconds=30),
        attributes={},
    )
    with db_conn.cursor() as cur:
        cur.execute(
            """
            WITH archive_agents AS (
                SELECT DISTINCT agent_id FROM events WHERE agent_id IS NOT NULL
            ),
            turn_distributions AS (
                SELECT agent_id, jsonb_agg(jsonb_build_array(value, count) ORDER BY value)
                    AS turn_distribution
                FROM (
                    SELECT agent_id, (attributes ->> 'duration_seconds')::float8 AS value,
                        count(*) AS count
                    FROM events
                    WHERE event_name ~ '^turn_end$'
                      AND category = ANY(ARRAY['telemetry', 'log'])
                      AND ts < (SELECT max(ts) FROM events)
                    GROUP BY agent_id, value
                ) grouped
                GROUP BY agent_id
            ),
            node_durations AS (
                SELECT agent_id,
                    COALESCE(
                        sum((attributes ->> 'duration_seconds')::float8)
                            FILTER (WHERE COALESCE(attributes ->> 'node', '') <> 'claim'),
                        0
                    ) AS active_seconds,
                    COALESCE(
                        sum((attributes ->> 'duration_seconds')::float8)
                            FILTER (WHERE attributes ->> 'node' = 'exec'),
                        0
                    ) AS exec_seconds
                FROM events
                WHERE event_name ~ '^node_exit$'
                  AND ts < (SELECT max(ts) FROM events)
                GROUP BY agent_id
            ),
            lifecycle_events AS (
                SELECT agent_id,
                    jsonb_agg(
                        jsonb_build_array(
                            to_char(ts AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"'),
                            event_name
                        )
                        ORDER BY ts
                    ) AS lifecycle
                FROM events
                WHERE event_name ~ '^(agent_spawned|agent_resurrected|agent_terminated)$'
                  AND ts < (SELECT max(ts) FROM events)
                GROUP BY agent_id
            )
            INSERT INTO agent_archive_stats (
                agent_id, turn_distribution, active_seconds, exec_seconds, lifecycle
            )
            SELECT archive_agents.agent_id,
                COALESCE(turn_distributions.turn_distribution, '[]'::jsonb),
                COALESCE(node_durations.active_seconds, 0),
                COALESCE(node_durations.exec_seconds, 0),
                COALESCE(lifecycle_events.lifecycle, '[]'::jsonb)
            FROM archive_agents
            LEFT JOIN turn_distributions USING (agent_id)
            LEFT JOIN node_durations USING (agent_id)
            LEFT JOIN lifecycle_events USING (agent_id)
            ON CONFLICT (agent_id) DO UPDATE SET
                turn_distribution = EXCLUDED.turn_distribution,
                active_seconds = EXCLUDED.active_seconds,
                exec_seconds = EXCLUDED.exec_seconds,
                lifecycle = EXCLUDED.lifecycle,
                computed_at = EXCLUDED.computed_at
            """
        )
    db_conn.commit()

    assert _inspect_pg.archive_stats(db_conn, agent_id=agent_id) == _inspect_pg.ArchiveStats(
        turn_distribution=_inspect_pg.archive_distribution(
            db_conn,
            field="duration_seconds",
            agent_id=agent_id,
            event_names=["^turn_end$"],
            categories=["telemetry", "log"],
            attribute_filters=None,
            from_=None,
            to=None,
        ),
        active_seconds=_inspect_pg.archive_aggregate(
            db_conn,
            field="duration_seconds",
            agg="sum",
            agent_id=agent_id,
            event_names=["^node_exit$"],
            categories=None,
            attribute_filters={"node": "!=claim"},
            from_=None,
            to=None,
        ),
        exec_seconds=_inspect_pg.archive_aggregate(
            db_conn,
            field="duration_seconds",
            agg="sum",
            agent_id=agent_id,
            event_names=["^node_exit$"],
            categories=None,
            attribute_filters={"node": "exec"},
            from_=None,
            to=None,
        ),
        lifecycle=_inspect_pg.archive_lifecycle(db_conn, agent_id=agent_id, from_=None, to=None),
    )
    assert _inspect_pg.archive_stats(db_conn, agent_id=no_archive_agent_id) is None


def test_inspect_values_whole_life_prefers_rollup_over_raw_scan(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_id = _insert_agent(db_conn)
    now = datetime.now(tz=UTC)
    _insert_event(
        db_conn,
        agent_id=agent_id,
        event_name="turn_end",
        ts=now - timedelta(minutes=2),
        attributes={"duration_seconds": 1.0},
    )
    _insert_event(
        db_conn,
        agent_id=agent_id,
        event_name="turn_end",
        ts=now - timedelta(minutes=1),
        attributes={"duration_seconds": 2.0},
    )
    _insert_event(
        db_conn,
        agent_id=agent_id,
        event_name="freeze_marker",
        ts=now - timedelta(seconds=30),
        attributes={},
    )
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_archive_stats (agent_id, turn_distribution) VALUES (%s, %s::jsonb)",
            (agent_id, Jsonb([[41.5, 2]])),
        )
    db_conn.commit()

    def raw_archive_read_must_not_run(*_args: Any, **_kwargs: Any) -> Never:
        raise AssertionError("whole-life inspect must use the archive rollup")

    monkeypatch.setattr(_inspect_pg, "archive_distribution", raw_archive_read_must_not_run)
    monkeypatch.setattr(_inspect_pg, "archive_aggregate", raw_archive_read_must_not_run)
    monkeypatch.setattr(_inspect_pg, "archive_lifecycle", raw_archive_read_must_not_run)

    def no_projected_lines(**_kwargs: Any) -> list[tuple[int, int | None, str]]:
        return []

    monkeypatch.setattr(_inspect_stats.loki_events, "query_projected_lines", no_projected_lines)

    with ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=1) as pool:
        values = _inspect_stats.inspect_values(pool, agent_id, None, None)

    assert values.stats.turn_p50_seconds == 41.5
    assert values.stats.turn_p90_seconds == 41.5
    assert values.stats.turn_min_seconds == 41.5
    assert values.stats.turn_max_seconds == 41.5


def test_inspect_values_windowed_uses_rollup_lifecycle(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_id = _insert_agent(db_conn)
    now = datetime.now(tz=UTC)
    _insert_event(
        db_conn,
        agent_id=agent_id,
        event_name="agent_spawned",
        ts=now - timedelta(hours=3),
        attributes={},
    )
    _insert_event(
        db_conn,
        agent_id=agent_id,
        event_name="turn_end",
        ts=now - timedelta(minutes=45),
        attributes={"duration_seconds": 4.5},
    )
    _insert_event(
        db_conn,
        agent_id=agent_id,
        event_name="freeze_marker",
        ts=now - timedelta(minutes=30),
        attributes={},
    )
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_archive_stats (agent_id, turn_distribution, lifecycle) "
            "VALUES (%s, %s::jsonb, %s::jsonb)",
            (
                agent_id,
                Jsonb([[99.0, 1]]),
                Jsonb([["2026-08-12T01:02:03.123456Z", "agent_resurrected"]]),
            ),
        )
    db_conn.commit()

    def no_projected_lines(**_kwargs: Any) -> list[tuple[int, int | None, str]]:
        return []

    monkeypatch.setattr(_inspect_stats.loki_events, "query_projected_lines", no_projected_lines)
    with ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=1) as pool:
        values = _inspect_stats.inspect_values(pool, agent_id, now - timedelta(hours=1), None)

    assert values.lifecycle_events == [
        (datetime(2026, 8, 12, 1, 2, 3, 123456, tzinfo=UTC), "agent_resurrected")
    ]
    assert values.stats.turn_p50_seconds == 4.5
    assert values.stats.turn_p90_seconds == 4.5


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
