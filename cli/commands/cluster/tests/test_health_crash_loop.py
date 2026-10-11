"""Crash-loop probes count the existing audit rows without release mutations."""

from collections.abc import Callable
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from base.db.tests.fakes import patch_database
from cli.commands.cluster import health as cluster_health
from tests.path_scoped.cli_tests import operator_database as operator_database


def _record_resurrect(
    db: psycopg.Connection, agent_id: int, *, minutes_ago: float, table: str = "audit_events"
) -> None:
    if table == "audit_events":
        db.execute(
            "INSERT INTO audit_events (event_uid, ts, agent_id, machine, process, event_name, "
            "level, source) VALUES (%s, now() - (%s * interval '1 minute'), %s, 'm', 'p', "
            "'resurrect', 'info', 'test')",
            (uuid4().int % (1 << 62), minutes_ago, agent_id),
        )
    else:  # the telemetry mirror of the same fact must not count
        db.execute(
            "INSERT INTO telemetry_events (event_uid, ts, agent_id, machine, cluster, process, "
            "category, event_name, level, source) VALUES (%s, now() - (%s * interval '1 minute'), "
            "%s, 'm', 'c', 'p', 'telemetry', 'resurrect', 'info', 'test')",
            (uuid4().int % (1 << 62), minutes_ago, agent_id),
        )
    db.commit()


def test_crash_loop_counts_the_audit_resurrect_rows_of_the_window(
    db_conn: psycopg.Connection, operator_database: Callable[[], Any]
) -> None:
    """Only audit resurrects in the window count; telemetry mirrors and old rows do not."""
    for _ in range(2):
        _record_resurrect(db_conn, 1, minutes_ago=10)
    _record_resurrect(db_conn, 1, minutes_ago=300)  # outside the window
    _record_resurrect(db_conn, 1, minutes_ago=5, table="telemetry_events")

    assert (
        cluster_health._crash_loop_detection(
            max_restarts=2, window_minutes=60, database_factory=operator_database
        )
        is True
    )

    _record_resurrect(db_conn, 1, minutes_ago=3)
    assert (
        cluster_health._crash_loop_detection(
            max_restarts=2, window_minutes=60, database_factory=operator_database
        )
        is False
    )


def test_crash_loop_is_judged_per_agent(
    db_conn: psycopg.Connection, operator_database: Callable[[], Any]
) -> None:
    for agent_id in (1, 2, 3):
        for _ in range(2):
            _record_resurrect(db_conn, agent_id, minutes_ago=10)

    assert (
        cluster_health._crash_loop_detection(
            max_restarts=2, window_minutes=60, database_factory=operator_database
        )
        is True
    )


def test_crash_loop_is_healthy_when_the_database_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch, operator_database: Callable[[], Any]
) -> None:

    def unreachable(**_kwargs: object) -> object:
        raise OSError("database unreachable")

    patch_database(monkeypatch, connect=unreachable)

    assert (
        cluster_health._crash_loop_detection(
            max_restarts=0, window_minutes=60, database_factory=operator_database
        )
        is True
    )
