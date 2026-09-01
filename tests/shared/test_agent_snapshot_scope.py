"""Query-shape contract for the bounded agent roster scopes."""

from __future__ import annotations

from typing import Any, cast

import psycopg
import pytest

from shared.agent_snapshot import AgentListScope, select_all


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
