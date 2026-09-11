"""Integration tests for `shared/machine_exclusions.py` — the operator-exclusion
read the deploy window consults (issue #2160).

Rows are inserted directly so each latch is exactly the written one, including a
machine that must NOT appear; the autouse per-test truncate covers `machines`.
"""

from __future__ import annotations

import psycopg

from shared import machine_exclusions


def test_reports_each_latch_with_reason_and_date(db_conn: psycopg.Connection) -> None:
    """One row per excluded machine, each carrying its own latch's date; active
    rows are absent and the staging flag (no date column) reads None."""
    db_conn.execute("INSERT INTO machines (name) VALUES ('live')")
    db_conn.execute(
        "INSERT INTO machines (name, paused_at) VALUES ('away', now() - interval '2 days')"
    )
    db_conn.execute(
        "INSERT INTO machines (name, stopped_at) VALUES ('gone', now() - interval '1 hour')"
    )
    db_conn.execute("INSERT INTO machines (name, is_staging) VALUES ('stage', true)")
    db_conn.commit()

    rows = machine_exclusions.list_excluded_machines()
    assert [(name, reason) for name, reason, _since in rows] == [
        ("away", "paused"),
        ("gone", "stopped"),
        ("stage", "staging"),
    ]
    dates = {name: since for name, _reason, since in rows}
    assert dates["stage"] is None
    assert dates["away"] is not None and dates["gone"] is not None
    assert dates["away"] < dates["gone"]  # each date is its own latch's


def test_the_pause_latch_outranks_a_stop_on_the_same_row(db_conn: psycopg.Connection) -> None:
    """The win shape (paused, then stopped): the pause latch is the one with an
    operator exit (`ava cluster resume`), so it is the one reported."""
    db_conn.execute(
        "INSERT INTO machines (name, paused_at, stopped_at) "
        "VALUES ('win', now() - interval '2 hours', now())"
    )
    db_conn.commit()

    assert [(name, reason) for name, reason, _s in machine_exclusions.list_excluded_machines()] == [
        ("win", "paused")
    ]
