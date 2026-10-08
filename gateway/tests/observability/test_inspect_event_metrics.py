"""`_event_metrics.points` evaluates the registry's LogQL templates on the event record."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from gateway.inspect import _event_metrics, _plugin_metrics

_STOP = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
_START = _STOP - timedelta(hours=2)
_SELECTOR = '{service_name="unknown_service", event_name="turn_end"}'


def _put(
    db: psycopg.Connection,
    name: str,
    *,
    minutes_before_stop: float,
    table: str = "telemetry_events",
    agent_id: int = 7,
    level: str = "info",
    attributes: dict[str, Any] | None = None,
) -> None:
    columns = "(event_uid, ts, agent_id, machine, process, event_name, level, source, attributes"
    values = "VALUES (%s, %s - (%s * interval '1 minute'), %s, 'm', 'p', %s, %s, 'test', %s::jsonb"
    extra_columns, extra_values = (
        (", category, cluster", ", 'telemetry', 'c'") if table == "telemetry_events" else ("", "")
    )
    query = f"INSERT INTO {table} {columns}{extra_columns}) {values}{extra_values})"
    db.execute(
        query,  # type: ignore[arg-type]
        (
            uuid.uuid4().int % (1 << 62),
            _STOP,
            minutes_before_stop,
            agent_id,
            name,
            level,
            json.dumps(attributes or {}),
        ),
    )
    db.commit()


def _eval(db: psycopg.Connection, query: str) -> dict[datetime, float]:
    return dict(_event_metrics.points(db, query, _START, _STOP, 3600))


def test_a_ratio_counts_each_window_that_ends_at_a_step(db_conn: psycopg.Connection) -> None:
    _put(db_conn, "turn_end", minutes_before_stop=10, attributes={"ok": True})
    _put(db_conn, "turn_end", minutes_before_stop=20, attributes={"ok": False})
    _put(db_conn, "turn_end", minutes_before_stop=30, attributes={"ok": True})
    _put(db_conn, "turn_end", minutes_before_stop=100, attributes={"ok": True})  # previous hour

    ratio = _eval(
        db_conn,
        f'100 * sum(count_over_time({_SELECTOR} | json | category="telemetry" | '
        f'attributes_ok="true" [1h])) / sum(count_over_time({_SELECTOR} | json | '
        'category="telemetry" [1h]))',
    )

    assert ratio[_STOP] == pytest.approx(200 / 3)
    assert ratio[_STOP - timedelta(hours=1)] == pytest.approx(100.0)
    assert _START not in ratio  # no row in its window: the denominator is zero


def test_a_constant_divisor_and_a_window_shorter_than_the_step(db_conn: psycopg.Connection) -> None:
    _put(db_conn, "turn_end", minutes_before_stop=2)
    _put(db_conn, "turn_end", minutes_before_stop=3)
    _put(db_conn, "turn_end", minutes_before_stop=30)  # outside the 5m window

    rate = _eval(
        db_conn,
        f'sum(count_over_time({_SELECTOR} | json | category="telemetry" [5m])) / 5',
    )

    assert rate[_STOP] == pytest.approx(0.4)
    assert rate[_STOP - timedelta(hours=1)] == 0.0


def test_unwrap_sums_the_numeric_attribute_and_skips_missing_and_text(
    db_conn: psycopg.Connection,
) -> None:
    _put(db_conn, "turn_end", minutes_before_stop=5, attributes={"cost_usd": 1.5})
    _put(db_conn, "turn_end", minutes_before_stop=6, attributes={"cost_usd": "2.5"})
    _put(db_conn, "turn_end", minutes_before_stop=7, attributes={"cost_usd": "n/a"})
    _put(db_conn, "turn_end", minutes_before_stop=8)

    total = _eval(
        db_conn,
        f'sum(sum_over_time({_SELECTOR} | json | category="telemetry" | '
        "unwrap attributes_cost_usd [1h]))",
    )

    assert total[_STOP] == pytest.approx(4.0)


def test_filters_on_level_agent_regex_and_not_equal(db_conn: psycopg.Connection) -> None:
    _put(db_conn, "turn_end", minutes_before_stop=5, agent_id=7, attributes={"body": "-> 0 kept"})
    _put(db_conn, "turn_end", minutes_before_stop=6, agent_id=7, attributes={"body": "-> 3 kept"})
    _put(db_conn, "turn_end", minutes_before_stop=7, agent_id=8, attributes={"body": "-> 0 kept"})
    _put(db_conn, "turn_end", minutes_before_stop=8, agent_id=7, level="error")

    def count(stages: str) -> float:
        return _eval(
            db_conn,
            f'sum(count_over_time({_SELECTOR} | json | category="telemetry" | {stages} [1h]))',
        )[_STOP]

    assert count('attributes_body =~ ".*-> 0 kept.*"') == 2
    assert count('attributes_body =~ ".*-> 0 kept.*" | agent_id="7"') == 1
    assert count('level="error"') == 1
    assert count('attributes_body!=""') == 3
    # a row with no body is not a match, so it stays in
    assert count('attributes_body !~ ".*0 kept.*" | agent_id="7"') == 2


def test_an_audit_category_reads_audit_events(db_conn: psycopg.Connection) -> None:
    _put(
        db_conn,
        "task_update",
        minutes_before_stop=5,
        table="audit_events",
        attributes={"status": "done"},
    )
    _put(
        db_conn,
        "task_update",
        minutes_before_stop=6,
        table="audit_events",
        attributes={"status": "open"},
    )
    selector = '{service_name="unknown_service", event_name="task_update"}'

    done = _eval(
        db_conn,
        f'sum(count_over_time({selector} | json | category="audit" | attributes_status="done" [1h]))',
    )

    assert done[_STOP] == 1


@pytest.mark.parametrize(
    "query",
    [
        'rate({service_name="unknown_service"} | json [1h])',
        f'sum(count_over_time({_SELECTOR} | json | line_format "x" [1h]))',
        f'sum(count_over_time({_SELECTOR} | json | unknown_field="x" [1h]))',
        f"sum(sum_over_time({_SELECTOR} | json [1h]))",
        f'sum(count_over_time({_SELECTOR} | json | category!="audit" [1h]))',
        f"sum(count_over_time({_SELECTOR} | json [1h])) + sum(count_over_time({_SELECTOR} | json [1h]))",
    ],
)
def test_anything_outside_the_vocabulary_is_refused(
    db_conn: psycopg.Connection, query: str
) -> None:
    with pytest.raises(_event_metrics.UnsupportedQueryError):
        _event_metrics.points(db_conn, query, _START, _STOP, 3600)


def test_every_shipped_inspector_metric_is_evaluable(db_conn: psycopg.Connection) -> None:
    specs = [s for s in _plugin_metrics._load_plugin_metrics() if "inspector" in s.output]
    assert specs
    for spec in specs:
        query = _plugin_metrics._translate_macros(
            _plugin_metrics._render_metric_query(spec, 7), logql=spec.query_type == "logql"
        )
        if spec.query_type == "logql":
            _event_metrics.points(db_conn, query, _START, _STOP, 3600)
