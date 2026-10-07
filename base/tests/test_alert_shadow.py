"""Real transaction regressions for inactive alert notification facts."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

from base.telemetry.alerts import AlertKey, notify_group_text, stamp_notified, upsert_alert
from base.telemetry.alerts.shadow import AlertShadowBatch


def alert(fp: str, *, status: str = "firing", severity: str = "error") -> dict[str, Any]:
    return {
        "fingerprint": fp,
        "starts_at": "2026-10-07T10:00:00Z",
        "status": status,
        "labels": {"alertname": "rule", "severity": severity},
        "annotations": {"summary": fp},
    }


def ingest(
    conn: psycopg.Connection,
    alerts: list[dict[str, Any]],
    *,
    mark_legacy_notified: bool = False,
    fail: str = "",
) -> None:
    with conn.transaction():
        batch = AlertShadowBatch(conn, alerts, "en")
        keys: list[AlertKey] = []
        for item in batch.items:
            key, previous = batch.observe(item)
            if key is None:
                continue
            _, _, should_notify, row = upsert_alert(conn, item, instance_key=key)
            batch.record(item, row, previous, should_notify=should_notify)
            keys.append(key)
        if fail == "before_freeze":
            raise RuntimeError("injected before freeze")
        batch.freeze()
        if fail == "after_freeze":
            raise RuntimeError("injected after freeze")
        if mark_legacy_notified:
            stamp_notified(conn, keys)


def facts(conn: psycopg.Connection) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT m.*,g.text,g.language,g.origin FROM alert_notification_members m "
            "JOIN alert_notification_groups g ON g.id=m.group_id ORDER BY group_id,ordinal"
        )
        return cur.fetchall()


def test_repeat_lost_response_and_changed_members_preserve_frozen_group(
    db_conn: psycopg.Connection,
) -> None:
    a, b, c = alert("a"), alert("b"), alert("c")
    ingest(db_conn, [a, b])  # Commit succeeds; caller may lose its response.
    original = facts(db_conn)
    db_conn.commit()
    changed = deepcopy(a)
    changed["annotations"]["summary"] = "new observation, same operation"
    changed["labels"]["severity"] = "critical"
    ingest(db_conn, [changed, b, c])
    current = facts(db_conn)
    assert current[:2] == original
    assert current[2]["group_id"] != current[0]["group_id"]
    assert [row["fingerprint"] for row in current] == ["a", "b", "c"]
    assert current[0]["text"] == notify_group_text([a, b], "en")
    assert db_conn.execute("SELECT severity FROM alerts WHERE fingerprint='a'").fetchone() == (
        "critical",
    )
    assert current[2]["text"] == notify_group_text([c], "en")
    assert all(row["origin"] == "shadow" for row in current)
    assert db_conn.execute(
        "SELECT count(*) FROM alerts WHERE notified_at IS NOT NULL"
    ).fetchone() == (0,)
    assert db_conn.execute(
        "SELECT annotations->>'summary' FROM alerts WHERE fingerprint='a'"
    ).fetchone() == ("new observation, same operation",)


@pytest.mark.parametrize("fail", ["before_freeze", "after_freeze"])
def test_fact_failures_roll_back_rows_revisions_and_group_members(
    db_conn: psycopg.Connection, fail: str
) -> None:
    with pytest.raises(RuntimeError, match="injected"):
        ingest(db_conn, [alert("a"), alert("b")], fail=fail)
    for table in ("alerts", "alert_notification_groups", "alert_notification_members"):
        assert db_conn.execute(
            sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
        ).fetchone() == (0,)


@pytest.mark.parametrize("opposite", [False, True])
def test_concurrent_absent_instances_and_opposite_key_order_create_one_fact(
    db_conn: psycopg.Connection, opposite: bool
) -> None:
    barrier = Barrier(2)
    items = [alert("a"), alert("b")]
    dsn = db_conn.info.dsn

    def run(reverse: bool) -> None:
        with psycopg.connect(dsn) as conn:
            conn.execute("SET lock_timeout='5s'")
            conn.commit()
            barrier.wait(timeout=5)
            ingest(conn, list(reversed(items)) if reverse else items)

    with ThreadPoolExecutor(max_workers=2) as executor:
        pending = [executor.submit(run, False), executor.submit(run, opposite)]
        for result in pending:
            result.result(timeout=10)
    current = facts(db_conn)
    assert len(current) == 2
    assert len({row["group_id"] for row in current}) == 1
    assert db_conn.execute(
        "SELECT array_agg(notification_revision ORDER BY fingerprint) FROM alerts"
    ).fetchone() == ([1, 1],)


def test_legacy_unconfirmed_instance_is_not_presented_as_fresh_history(
    db_conn: psycopg.Connection,
) -> None:
    item = alert("legacy")
    upsert_alert(db_conn, item)
    db_conn.commit()
    ingest(db_conn, [item])
    assert facts(db_conn)[0]["reason"] == "legacy_unconfirmed"
    db_conn.commit()
    ingest(db_conn, [item])
    assert len(facts(db_conn)) == 1


def test_policy_transitions_have_distinct_revisions_and_same_observation_has_one(
    db_conn: psycopg.Connection,
) -> None:
    ingest(db_conn, [alert("a", severity="warning")] * 2, mark_legacy_notified=True)
    ingest(db_conn, [alert("a", severity="critical")], mark_legacy_notified=True)
    ingest(db_conn, [alert("a", status="resolved", severity="critical")])
    ingest(db_conn, [alert("a", severity="critical")])
    current = facts(db_conn)
    assert [row["reason"] for row in current] == [
        "fresh_firing",
        "escalation",
        "resolution",
        "refire",
    ]
    assert [row["notification_revision"] for row in current] == [1, 2, 3, 4]
    assert len({row["group_id"] for row in current}) == 4


def test_equivalent_timezone_and_duplicate_order_recover_one_revision(
    db_conn: psycopg.Connection,
) -> None:
    a = alert("a")
    same = deepcopy(a)
    same["starts_at"] = "2026-10-07T18:00:00+08:00"
    ingest(db_conn, [a, same])
    assert len(facts(db_conn)) == 1


def test_existing_start_fallback_and_unknown_start_keep_current_policy(
    db_conn: psycopg.Connection,
) -> None:
    a = alert("a")
    ingest(db_conn, [a])
    a["starts_at"] = ""
    unknown = alert("unknown")
    unknown["starts_at"] = ""
    ingest(db_conn, [a, unknown])
    assert len(facts(db_conn)) == 1
    assert db_conn.execute("SELECT count(*) FROM alerts").fetchone() == (1,)


def test_missing_start_lookup_waits_for_concurrent_latest_instance(
    db_conn: psycopg.Connection,
) -> None:
    """Lookup happens after the fingerprint gate, seeing the committed latest instance."""
    import time
    from threading import Event

    ingest(db_conn, [alert("a")])
    latest = alert("a")
    latest["starts_at"] = "2026-10-07T11:00:00Z"
    missing = alert("a")
    missing["starts_at"] = ""
    missing["annotations"]["summary"] = "latest observation"
    db_conn.commit()
    dsn = db_conn.info.dsn
    started = Event()
    second_pid: list[int] = []

    def lookup() -> None:
        with psycopg.connect(dsn) as conn:
            conn.execute("SET lock_timeout='5s'")
            conn.commit()
            second_pid.append(conn.info.backend_pid)
            started.set()
            ingest(conn, [missing])

    with psycopg.connect(dsn) as first, ThreadPoolExecutor(max_workers=1) as executor:
        with first.transaction():
            batch = AlertShadowBatch(first, [latest], "en")
            key, old = batch.observe(latest)
            assert key is not None
            _, _, notify, row = upsert_alert(first, latest, instance_key=key)
            batch.record(latest, row, old, should_notify=notify)
            batch.freeze()
            future = executor.submit(lookup)
            assert started.wait(timeout=5)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                waiting = db_conn.execute(
                    "SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s",
                    (second_pid[0],),
                ).fetchone()
                db_conn.commit()  # Refresh pg_stat_activity rather than reusing its snapshot.
                if waiting == ("Lock",):
                    break
                time.sleep(0.01)
            else:
                pytest.fail("concurrent lookup did not wait for the fingerprint gate")
        future.result(timeout=10)
    assert len(facts(db_conn)) == 2
    assert db_conn.execute(
        "SELECT annotations->>'summary' FROM alerts ORDER BY starts_at"
    ).fetchall() == [
        ("a",),
        ("latest observation",),
    ]


def test_member_write_failure_rolls_back_previously_inserted_group_and_alert_revision(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from base.telemetry.alerts import shadow

    ingest(db_conn, [alert("a", severity="warning")], mark_legacy_notified=True)
    original = facts(db_conn)
    db_conn.commit()

    def fail_snapshot(value: object) -> None:
        assert db_conn.execute("SELECT count(*) FROM alert_notification_groups").fetchone() == (2,)
        raise RuntimeError("injected member write failure")

    monkeypatch.setattr(shadow, "Jsonb", fail_snapshot)
    with pytest.raises(RuntimeError, match="member write failure"):
        ingest(db_conn, [alert("a", severity="critical")])
    assert facts(db_conn) == original
    assert db_conn.execute("SELECT notification_revision,severity FROM alerts").fetchone() == (
        1,
        "warning",
    )
