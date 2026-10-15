from __future__ import annotations

import psycopg

from base.cluster import machine_exclusions
from base.db import Database
from base.db.code_version_gate import ProcessDbGate


def _db(database_gate: ProcessDbGate) -> Database:
    return Database.from_settings(gate=database_gate)


def test_reports_each_latch_with_reason_and_date(
    db_conn: psycopg.Connection, database_gate: ProcessDbGate
) -> None:
    db_conn.execute("INSERT INTO machines (name) VALUES ('live')")
    db_conn.execute(
        "INSERT INTO machines (name, paused_at) VALUES ('away', now() - interval '2 days')"
    )
    db_conn.execute(
        "INSERT INTO machines (name, stopped_at) VALUES ('gone', now() - interval '1 hour')"
    )
    db_conn.execute("INSERT INTO machines (name, is_staging) VALUES ('stage', true)")
    db_conn.commit()

    rows = machine_exclusions.list_excluded_machines(_db(database_gate=database_gate))
    assert [(name, reason) for name, reason, _since in rows] == [
        ("away", "paused"),
        ("gone", "stopped"),
        ("stage", "staging"),
    ]
    dates = {name: since for name, _reason, since in rows}
    assert dates["stage"] is None
    assert dates["away"] is not None and dates["gone"] is not None
    assert dates["away"] < dates["gone"]


def test_the_pause_latch_outranks_a_stop_on_the_same_row(
    db_conn: psycopg.Connection, database_gate: ProcessDbGate
) -> None:
    db_conn.execute(
        "INSERT INTO machines (name, paused_at, stopped_at) "
        "VALUES ('runner-a', now() - interval '2 hours', now())"
    )
    db_conn.commit()

    assert [
        (name, reason)
        for name, reason, _s in machine_exclusions.list_excluded_machines(
            _db(database_gate=database_gate)
        )
    ] == [("runner-a", "paused")]
