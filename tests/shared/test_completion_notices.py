"""Completion-notice policy and digest contracts."""

from __future__ import annotations

from datetime import UTC, datetime

import psycopg

from shared.completion_notices import (
    CompletionNotice,
    delivery_required_for_agent,
    format_digest,
    immediate_delivery_required,
)


def test_hourly_keeps_failures_immediate_and_counts_them_in_the_digest() -> None:
    success = CompletionNotice(
        source="shell:1",
        content="Background command 'build' exited with code 0. Full output at build.log.",
        outcome="exit",
        exit_code=0,
    )
    failure = CompletionNotice(
        source="watcher:2",
        content="Watcher 'check' exited with code 1. Full output at check.log.",
        outcome="exit",
        exit_code=1,
    )

    assert not immediate_delivery_required("hourly", success)
    assert immediate_delivery_required("hourly", failure)

    digest = format_digest(
        agent_id=7,
        window_start=datetime(
            2026, 9, 22, 10, tzinfo=UTC
        ),  # time-bomb-ok: fixed UTC formatting contract
        notices=[success, failure],
    )

    assert "2 completion notices" in digest
    assert "build.log" in digest
    assert "Recent failures (already delivered immediately; latest 5):" in digest
    assert "check.log" in digest


def test_failures_policy_suppresses_only_successes() -> None:
    assert not immediate_delivery_required(
        "failures",
        CompletionNotice(source="shell:1", content="ok", outcome="exit", exit_code=0),
    )
    assert immediate_delivery_required(
        "failures",
        CompletionNotice(source="shell:1", content="failed", outcome="exit", exit_code=1),
    )
    assert immediate_delivery_required(
        "failures",
        CompletionNotice(source="watcher:1", content="missed", outcome="missed"),
    )


def test_digest_bounds_rendered_logs_but_keeps_the_total_count() -> None:
    notices = [
        CompletionNotice(
            source=f"shell:{index}",
            content=f"Background command '{index}' exited with code 0. Full output at {index}.log.",
            outcome="exit",
            exit_code=0,
        )
        for index in range(23)
    ]
    digest = format_digest(
        agent_id=7,
        window_start=datetime(
            2026, 9, 22, 10, tzinfo=UTC
        ),  # time-bomb-ok: fixed UTC formatting contract
        notices=notices,
    )
    assert "23 completion notices" in digest
    assert "3 older successful completion(s) omitted" in digest
    assert "at 0.log." not in digest
    assert "at 22.log." in digest


def test_buffered_success_stays_suppressed_after_a_policy_flip(
    db_conn: psycopg.Connection,
) -> None:
    """A failed response replay cannot duplicate a success buffered under hourly."""
    from shared.db import create_agent

    agent_id = create_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status, config_overlay) "
            "VALUES (%s, 'test', 'idling', %s::jsonb)",
            (agent_id, '{"completion_notice_policy": "hourly"}'),
        )
    db_conn.commit()
    notice = CompletionNotice(
        source="shell:91",
        content="Background command 'build' exited with code 0. Full output at build.log.",
        outcome="exit",
        exit_code=0,
    )
    assert not delivery_required_for_agent(db_conn, agent_id, notice, "all")
    db_conn.commit()
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET config_overlay = %s::jsonb WHERE id = %s",
            ('{"completion_notice_policy": "all"}', agent_id),
        )
    db_conn.commit()
    assert not delivery_required_for_agent(db_conn, agent_id, notice, "all")
