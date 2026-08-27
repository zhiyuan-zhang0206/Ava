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
