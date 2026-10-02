"""A machine pause resolves the old and the new fingerprint alerts."""

from __future__ import annotations

import psycopg

from ops.tests.pool_support import make_test_pool


def test_machine_pause_resolves_old_and_new_fingerprint_alerts(
    db_conn: psycopg.Connection,
) -> None:
    """Expected absence closes every open episode across fingerprint conventions."""
    from datetime import UTC, datetime

    from psycopg.types.json import Jsonb

    from base.telemetry.alerts import fingerprint
    from gateway.cluster.machine_pause import _resolve_machine_alerts_blocking

    identity_labels = {"alertname": "machine offline", "machine": "away"}
    old_labels = {**identity_labels, "severity": "warning"}
    new_labels = {**identity_labels, "severity": "error"}
    old_start = datetime(2026, 8, 5, tzinfo=UTC)
    new_start = datetime(2026, 8, 26, tzinfo=UTC)
    with db_conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO alerts (status, severity, alertname, labels, annotations, "
            "starts_at, fingerprint, source, notified_at) VALUES "
            "('unresolved', %s, 'machine offline', %s, '{}', %s, %s, "
            "'machine-probe', now())",
            [
                ("warning", Jsonb(old_labels), old_start, fingerprint(old_labels)),
                ("error", Jsonb(new_labels), new_start, fingerprint(identity_labels)),
            ],
        )
    db_conn.commit()

    with make_test_pool() as pool:
        _resolve_machine_alerts_blocking(pool, "away")

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT status, severity, labels->>'severity', fingerprint, ends_at "
            "FROM alerts WHERE labels->>'machine' = 'away' ORDER BY starts_at"
        )
        rows = cur.fetchall()
    assert [(row[0], row[1], row[2], row[3]) for row in rows] == [
        ("resolved", "warning", "warning", fingerprint(old_labels)),
        ("resolved", "error", "error", fingerprint(identity_labels)),
    ]
    assert all(row[4] is not None for row in rows)
