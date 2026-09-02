"""Query-shape contract for the bounded agent roster scopes."""

from __future__ import annotations

from typing import Any, cast

import psycopg
import pytest

from shared.agent_snapshot import (
    AgentListScope,
    AgentListSummary,
    AgentSnapshot,
    select_all,
)


class _RecordingCursor:
    def __init__(self) -> None:
        self.query = ""

    def __enter__(self) -> _RecordingCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str) -> None:
        self.query = query

    def fetchall(self) -> list[tuple[Any, ...]]:
        return []


class _RecordingConnection:
    def __init__(self) -> None:
        self.recording_cursor = _RecordingCursor()

    def cursor(self) -> _RecordingCursor:
        return self.recording_cursor


def _seed_agents_meta(
    db_conn: psycopg.Connection,
    rows: list[tuple[int, str, str]],
) -> None:
    """Insert agent rows with explicit lineage for roster projection tests."""
    with db_conn.cursor() as cur:
        for agent_id, spawner, status in rows:
            cur.execute("INSERT INTO agents (id) VALUES (%s)", (agent_id,))
            cur.execute(
                "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, %s, %s)",
                (agent_id, spawner, status),
            )
    db_conn.commit()


def _spawners_for_scope(
    db_conn: psycopg.Connection,
    scope: AgentListScope,
) -> dict[int, str]:
    """Return the observable roster lineage indexed by agent id."""
    snapshots = cast(list[AgentSnapshot], select_all(db_conn, scope=scope))
    return {agent.agent_id: agent.spawner for agent in snapshots}


def _summary_spawners_for_scope(
    db_conn: psycopg.Connection,
    scope: AgentListScope,
) -> dict[int, str]:
    """Return the frontend roster's observable lineage indexed by agent id."""
    summaries = cast(
        list[AgentListSummary],
        select_all(db_conn, scope=scope, fields="summary"),
    )
    return {agent.agent_id: agent.spawner for agent in summaries}


@pytest.mark.parametrize(
    ("scope", "predicate"),
    [
        ("live", "WHERE a.status <> 'terminated'"),
        ("terminated", "WHERE a.status = 'terminated'"),
    ],
)
def test_scoped_roster_filter_is_part_of_the_sql(
    scope: AgentListScope,
    predicate: str,
) -> None:
    """Filtering after fetch would reintroduce the historical O(agents) scan."""
    conn = _RecordingConnection()
    select_all(cast(psycopg.Connection[Any], conn), scope=scope)
    assert predicate in conn.recording_cursor.query


def test_all_scope_preserves_the_unfiltered_compatibility_query() -> None:
    conn = _RecordingConnection()
    select_all(cast(psycopg.Connection[Any], conn))
    assert "WHERE a.status" not in conn.recording_cursor.query


def test_summary_projection_omits_detail_only_columns_in_sql() -> None:
    """The list summary must not transfer fields that only detail/SSE readers use."""
    conn = _RecordingConnection()
    select_all(cast(psycopg.Connection[Any], conn), scope="live", fields="summary")
    query = conn.recording_cursor.query
    selected_columns = query.removeprefix("SELECT ").partition(" FROM agents_meta a ")[0]

    assert selected_columns == (
        "a.id, a.spawner, a.fork_source_agent_id, a.status, a.pid, "
        "a.spawned_at, a.started_at, "
        "COALESCE(a.last_active_at, a.started_at, a.spawned_at) AS last_active_at, "
        "COALESCE(im.last_inbound_at, a.started_at, a.spawned_at) AS last_inbound_at, "
        "t.label, a.machine, a.heartbeat_paused_until, a.liveness_state, "
        "COALESCE("
        "(SELECT json_agg(json_build_object("
        "'id', n.id, 'title', n.title, 'content', n.content, 'priority', n.priority, "
        "'blocking', n.blocking, 'created_at', n.created_at, 'task_id', n.task_id) ORDER BY n.created_at) "
        "FROM agent_notices n "
        "WHERE n.agent_id = a.id AND n.require_response AND n.resolved_at IS NULL), "
        "'[]'::json) AS notices_awaiting_response, "
        "(SELECT count(*) FROM agent_notices n "
        "WHERE n.agent_id = a.id AND NOT n.require_response AND n.resolved_at IS NULL "
        "AND n.created_at > now() - interval '30 days') AS unread_notice_count, "
        "a.config_overlay ->> 'llm_model' AS effective_model"
    )

    assert "WHERE a.status <> 'terminated'" in query
    assert "a.fork_source_agent_id" in query  # frontend fork-tree lineage
    assert "fork_source_checkpoint_id" not in query
    assert "last_probe_at" not in query
    assert "a.config_overlay," not in query
    assert "a.config_overlay ->> 'llm_model' AS effective_model" in query


@pytest.mark.parametrize(
    ("scope", "expected_query"),
    [
        (
            "all",
            "SELECT a.id, a.status, t.label FROM agents_meta a JOIN agents t ON t.id = a.id "
            "ORDER BY a.id",
        ),
        (
            "live",
            "SELECT a.id, a.status, t.label FROM agents_meta a JOIN agents t ON t.id = a.id "
            "WHERE a.status <> 'terminated' ORDER BY a.id",
        ),
        (
            "terminated",
            "SELECT a.id, a.status, t.label FROM agents_meta a JOIN agents t ON t.id = a.id "
            "WHERE a.status = 'terminated' ORDER BY a.id",
        ),
    ],
)
def test_compact_projection_reads_only_cli_columns_in_sql(
    scope: AgentListScope, expected_query: str
) -> None:
    """Every compact scope must avoid roster-only lookups for three columns."""
    conn = _RecordingConnection()
    select_all(cast(psycopg.Connection[Any], conn), scope=scope, fields="compact")
    assert conn.recording_cursor.query == expected_query


def test_live_roster_projects_dead_spawner_chains_to_nearest_live_ancestor(
    db_conn: psycopg.Connection,
) -> None:
    _seed_agents_meta(
        db_conn,
        [
            (228, "user", "running"),
            (1581, "agent:228", "terminated"),
            (2147, "agent:1581", "terminated"),
            (2894, "agent:2147", "running"),
            (240, "agent:228", "terminated"),
            (312, "agent:240", "running"),
            (405, "user", "running"),
            (5709, "agent:405", "running"),
        ],
    )

    spawners = _spawners_for_scope(db_conn, "live")

    assert spawners[2894] == "agent:228"
    assert spawners[312] == "agent:228"
    assert spawners[5709] == "agent:405"
    assert spawners[228] == "user"
    assert _summary_spawners_for_scope(db_conn, "live")[2894] == "agent:228"


def test_non_live_rosters_keep_raw_spawner_lineage(
    db_conn: psycopg.Connection,
) -> None:
    _seed_agents_meta(
        db_conn,
        [
            (228, "user", "running"),
            (1581, "agent:228", "terminated"),
            (2147, "agent:1581", "terminated"),
            (2894, "agent:2147", "running"),
        ],
    )

    assert _spawners_for_scope(db_conn, "all")[2894] == "agent:2147"

    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = 2894")
    db_conn.commit()

    assert _spawners_for_scope(db_conn, "terminated")[2894] == "agent:2147"


def test_live_roster_keeps_unresolvable_and_external_spawners(
    db_conn: psycopg.Connection,
) -> None:
    _seed_agents_meta(
        db_conn,
        [
            (777, "agent:778", "running"),
            (778, "user", "terminated"),
            (779, "agent:999999", "running"),
            (780, "claude-code", "running"),
            (781, "user", "running"),
            (782, "agent:783", "running"),
            (783, "agent:784", "terminated"),
            (784, "agent:783", "terminated"),
        ],
    )

    spawners = _spawners_for_scope(db_conn, "live")

    assert spawners[777] == "agent:778"
    assert spawners[779] == "agent:999999"
    assert spawners[780] == "claude-code"
    assert spawners[781] == "user"
    assert spawners[782] == "agent:783"


def test_live_roster_projection_uses_only_read_queries(
    db_conn: psycopg.Connection,
) -> None:
    _seed_agents_meta(
        db_conn,
        [
            (228, "user", "running"),
            (2147, "agent:228", "terminated"),
            (2894, "agent:2147", "running"),
        ],
    )

    with db_conn.transaction():
        db_conn.execute("SET TRANSACTION READ ONLY")
        assert _spawners_for_scope(db_conn, "live")[2894] == "agent:228"
