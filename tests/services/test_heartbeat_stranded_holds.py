"""services.heartbeat.stranded_holds — grading host records into alert edges.

Task #3132: the pass is the user-facing edge for the stranded-hold record a host
writes when a failed update leg leaves it held (ownerless maintenance hold).
Mirrors the machine-offline edge tests: real DB, IM mocked at
``shared.alerts.notify_im``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from services.heartbeat.stranded_holds import ALERTNAME, SOURCE, grade_stranded_holds
from shared.config import settings

_MACHINE = "test-held-1"
_SINCE = datetime(2026, 9, 12, 1, 2, tzinfo=timezone(timedelta(hours=8)))


@pytest.fixture
def pool():
    p = ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=2, open=True)
    try:
        yield p
    finally:
        p.close()


def _notify_into(sink: list[str]) -> Callable[[str], bool]:
    def _notify(text: str) -> bool:
        sink.append(text)
        return True

    return _notify


def _notify_true(_text: str) -> bool:
    return True


@pytest.fixture(autouse=True)
def _clean(db_conn: psycopg.Connection) -> Iterator[None]:
    """host_deploy_state is not in the conftest TRUNCATE list; this module
    self-manages its row, and the alert rows keyed to the test alertname."""

    def _wipe() -> None:
        with db_conn.cursor() as cur:
            cur.execute("DELETE FROM host_deploy_state WHERE machine = %s", (_MACHINE,))
            cur.execute("DELETE FROM alerts WHERE labels->>'alertname' = %s", (ALERTNAME,))
        db_conn.commit()

    _wipe()
    yield
    _wipe()


def _declare(db_conn: psycopg.Connection, *, reason: str = "updater exited rc=1") -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO host_deploy_state "
            "(machine, posture, paused_at, stranded_hold_since, stranded_hold_reason) "
            "VALUES (%s, 'paused', now(), %s, %s) "
            "ON CONFLICT (machine) DO UPDATE SET "
            "  stranded_hold_since = EXCLUDED.stranded_hold_since, "
            "  stranded_hold_reason = EXCLUDED.stranded_hold_reason",
            (_MACHINE, _SINCE, reason),
        )
    db_conn.commit()


def _clear_record(db_conn: psycopg.Connection) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE host_deploy_state SET stranded_hold_since = NULL, "
            "stranded_hold_reason = NULL WHERE machine = %s",
            (_MACHINE,),
        )
    db_conn.commit()


def _alerts(db: psycopg.Connection) -> list[tuple[object, ...]]:
    with db.cursor() as cur:
        cur.execute(
            "SELECT status, severity, labels->>'machine', notified_at, source, starts_at "
            "FROM alerts WHERE labels->>'alertname' = %s ORDER BY starts_at",
            (ALERTNAME,),
        )
        return cur.fetchall()


def test_declared_record_fires_one_alert_and_notifies(
    pool: ConnectionPool, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    notified: list[str] = []
    monkeypatch.setattr("shared.alerts.notify_im", _notify_into(notified))
    _declare(db_conn)
    grade_stranded_holds(pool)
    rows = _alerts(db_conn)
    assert len(rows) == 1
    status, severity, machine, notified_at, source, _starts = rows[0]
    assert (status, severity, machine, source) == ("unresolved", "error", _MACHINE, SOURCE)
    assert notified_at is not None
    assert notified and "ava start" in notified[0]
    assert "run `ava start`" in notified[0]


def test_repeat_passes_do_not_respam(
    pool: ConnectionPool, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr("shared.alerts.notify_im", _notify_into(calls))
    _declare(db_conn)
    grade_stranded_holds(pool)
    grade_stranded_holds(pool)
    assert len(calls) == 1


def test_failed_notify_is_retried_next_pass(
    pool: ConnectionPool, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A silent IM outage must not silence the alert: notified_at stays NULL and
    the next pass retries (the firing gate in upsert_alert)."""
    calls: list[str] = []
    ok = [False]

    def _notify(text: str) -> bool:
        calls.append(text)
        return ok[0]

    monkeypatch.setattr("shared.alerts.notify_im", _notify)
    _declare(db_conn)
    grade_stranded_holds(pool)
    assert _alerts(db_conn)[0][3] is None  # notified_at not stamped
    ok[0] = True
    grade_stranded_holds(pool)
    assert _alerts(db_conn)[0][3] is not None
    assert len(calls) == 2


def test_cleared_record_resolves_the_open_instance(
    pool: ConnectionPool, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shared.alerts.notify_im", _notify_true)
    _declare(db_conn)
    grade_stranded_holds(pool)
    _clear_record(db_conn)
    grade_stranded_holds(pool)
    rows = _alerts(db_conn)
    assert [row[0] for row in rows] == ["resolved"]
