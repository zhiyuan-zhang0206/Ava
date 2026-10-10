"""SQL and Python system-notice predicates share exact marker semantics."""

import psycopg

from services.wake.delivery_watchdog.tests.test_delivery_watchdog import (
    healthy_host_verdict as healthy_host_verdict,
)


class TestSystemNoticeSourcePredicateParity:
    """`SYSTEM_NOTICE_SOURCE` (SQL, consumed by the selector) and
    `is_system_notice_source` (Python, consumed by the resurrect endpoint)
    gate the same decision from two languages; they must agree on every
    `(source, payload)` input — a one-sided edit would reopen the 6260 gap
    from the other side (task #3687 review note: pin the pair together). The
    payload dimension covers the hosted-turn-recovery carve-out and its
    fail-closed marker rule: only the exact JSON boolean `true` exempts; a
    missing key, JSON null, or any other value (even the string "true") stays
    a notice (review requirement, Ava #3242)."""

    # (label, payload, exempt-from-notice-verdict)
    _PAYLOAD_SAMPLES: tuple[tuple[str, dict[str, object] | None, bool], ...] = (
        ("payload-absent", None, False),
        ("key-absent", {"content_blocks": []}, False),
        ("json-null", {"hosted_turn_recovery": None}, False),
        ("boolean-false", {"hosted_turn_recovery": False}, False),
        ("string-false", {"hosted_turn_recovery": "false"}, False),
        ("number-1", {"hosted_turn_recovery": 1}, False),
        ("string-1", {"hosted_turn_recovery": "1"}, False),
        ("string-true", {"hosted_turn_recovery": "true"}, False),
        ("boolean-true", {"hosted_turn_recovery": True}, True),
    )

    def test_sql_fragment_and_python_twin_agree(self, db_conn: psycopg.Connection) -> None:
        from psycopg import sql
        from psycopg.types.json import Jsonb

        from base.agents.incarnation.lifecycle_acceptance import (
            SYSTEM_NOTICE_SOURCE,
            is_system_notice_source,
        )

        sources = [
            "system",
            "system:",
            "system:notice-reply",
            "system:warn-error-audit",
            "systemwarn",
            "system%d",
            "system_x",
            "System",
            " system",
            "system :x",
            "",
            "user",
            "agent:1",
            "ui:web",
            "watcher:3",
            "shell:0",
            "schedule:2",
        ]
        with db_conn.cursor() as cur:
            for source in sources:
                for label, payload, _exempt in self._PAYLOAD_SAMPLES:
                    cur.execute(
                        sql.SQL(
                            "SELECT {} FROM (SELECT %s::text AS source, %s::jsonb AS payload) AS m"
                        ).format(sql.SQL(SYSTEM_NOTICE_SOURCE)),
                        (source, Jsonb(payload) if payload is not None else None),
                    )
                    row = cur.fetchone()
                    assert row is not None, (source, label)
                    assert row[0] == is_system_notice_source(source, payload), (source, label)

    def test_only_the_exact_boolean_true_marker_exempts(self) -> None:
        from base.agents.incarnation.lifecycle_acceptance import is_system_notice_source

        for source in ("system", "system:notice-reply"):
            for label, payload, exempt in self._PAYLOAD_SAMPLES:
                expected = not exempt
                assert is_system_notice_source(source, payload) is expected, (source, label)
        # The marker only ever applies inside the system family.
        assert is_system_notice_source("watcher:3", {"hosted_turn_recovery": True}) is False
