"""Completion-notice policy and digest contracts."""

from __future__ import annotations

from datetime import UTC, datetime

import psycopg

from base.daemon.schedules.completion_notices import (
    CompletionNotice,
    delivery_required_for_agent,
    format_digest,
    immediate_delivery_required,
)
from base.daemon.schedules.completion_policy import CompletionNoticePolicy


def test_hourly_buffers_every_notice_and_digest_counts_them() -> None:
    first = CompletionNotice(
        source="shell:1",
        content="Background command 'build' finished. Full output at build.log.",
    )
    second = CompletionNotice(
        source="watcher:2",
        content="Watcher 'check' finished. Full output at check.log.",
    )

    assert not immediate_delivery_required(CompletionNoticePolicy.HOURLY, first)
    assert not immediate_delivery_required(CompletionNoticePolicy.HOURLY, second)
    assert immediate_delivery_required(CompletionNoticePolicy.ALL, first)

    digest = format_digest(
        agent_id=7,
        window_start=datetime(
            2026, 9, 22, 10, tzinfo=UTC
        ),  # time-bomb-ok: fixed UTC formatting contract
        notices=[first, second],
    )

    assert "2 completion notices" in digest
    assert "build.log" in digest
    assert "check.log" in digest


def test_digest_bounds_rendered_logs_but_keeps_the_total_count() -> None:
    notices = [
        CompletionNotice(
            source=f"shell:{index}",
            content=f"Background command '{index}' finished. Full output at {index}.log.",
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
    assert "3 older completion(s) omitted" in digest
    assert "at 0.log." not in digest
    assert "at 22.log." in digest


def test_buffered_notice_stays_suppressed_after_a_policy_flip(
    db_conn: psycopg.Connection,
) -> None:
    """A failed response replay cannot duplicate a notice buffered under hourly."""
    from base.db import create_agent

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
        content="Background command 'build' finished. Full output at build.log.",
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
