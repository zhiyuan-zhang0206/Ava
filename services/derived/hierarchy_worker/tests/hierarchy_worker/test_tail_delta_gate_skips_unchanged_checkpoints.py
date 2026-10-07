"""Hierarchy worker cases: tail delta gate skips unchanged checkpoints."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from base.agents.history.hierarchy.pipeline import MaterializedTree
from base.config import settings
from services.derived.hierarchy_worker import execute as execute_module
from services.derived.hierarchy_worker.tests.slices import execute_job, scan
from services.derived.hierarchy_worker.tests.test_hierarchy_worker import (
    _boundary,
    _clean_compact_job,
    _plain_checkpoint,
    _state,
    cid,
)
from services.derived.hierarchy_worker.tests.test_hierarchy_worker import (
    _fake_generation_model as _fake_generation_model,
)


def test_tail_delta_gate_skips_unchanged_checkpoints(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.daemon, "hierarchy_tail_seal_enabled", True)
    now = datetime.now(UTC)
    agent_id = 880_105
    _state(db_conn, agent_id, cid(1))
    _clean_compact_job(db_conn, agent_id, cid(1))
    newest = _plain_checkpoint(db_conn, agent_id, now - timedelta(minutes=30), 9)
    db_conn.execute(
        "UPDATE hierarchy_worker_state SET last_tail_seal_cp_id = %s WHERE agent_id = %s",
        (newest, agent_id),
    )
    db_conn.commit()

    assert scan(db_conn).tail_enqueued == 0  # nothing written since the last seal


def test_tail_interval_paces_clean_seals_and_continuations_drain(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.daemon, "hierarchy_tail_seal_enabled", True)
    now = datetime.now(UTC)
    agent_id = 880_106
    _state(db_conn, agent_id, cid(1))
    _clean_compact_job(db_conn, agent_id, cid(1))
    _plain_checkpoint(db_conn, agent_id, now - timedelta(minutes=30), 9)
    db_conn.execute(
        "INSERT INTO hierarchy_jobs (agent_id, kind, trigger_boundary, status, include_tail,"
        " failed, skipped, finished_at)"
        " VALUES (%s, 'tail', %s, 'done', true, 0, 0, now() - interval '10 minutes')",
        (agent_id, cid(9)),
    )
    db_conn.commit()
    assert scan(db_conn).tail_enqueued == 0  # inside the clean-seal interval

    # A non-clean attempt waits out its backoff instead of the interval.
    db_conn.execute(
        "UPDATE hierarchy_jobs SET status = 'failed', error = 'boom', finished_at = now()"
        " WHERE agent_id = %s AND kind = 'tail'",
        (agent_id,),
    )
    db_conn.commit()
    assert scan(db_conn).tail_enqueued == 0  # inside the retry backoff

    # Past both gates a fresh seal is due.
    db_conn.execute(
        "UPDATE hierarchy_jobs SET finished_at = now() - interval '90 minutes'"
        " WHERE agent_id = %s AND kind = 'tail'",
        (agent_id,),
    )
    db_conn.commit()
    assert scan(db_conn).tail_enqueued == 1

    # A pure continuation (done, no failures, a skipped remainder) drains
    # immediately — the same rule as the compact channel.
    db_conn.execute(
        "UPDATE hierarchy_jobs SET status = 'done', error = NULL, failed = 0, skipped = 5,"
        " finished_at = now() WHERE agent_id = %s AND kind = 'tail' AND status = 'pending'",
        (agent_id,),
    )
    db_conn.commit()
    assert scan(db_conn).tail_enqueued == 1


def test_tail_tick_cap_limits_enqueues(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.daemon, "hierarchy_tail_seal_enabled", True)
    now = datetime.now(UTC)
    for agent_id in range(880_110, 880_114):
        _state(db_conn, agent_id, cid(1))
        _clean_compact_job(db_conn, agent_id, cid(1))
        _plain_checkpoint(db_conn, agent_id, now - timedelta(minutes=30), 9)

    assert scan(db_conn).tail_enqueued == settings.daemon.hierarchy_tail_max_per_tick
    # The cap is per tick: the next pass picks the remaining agent up.
    assert scan(db_conn).tail_enqueued == 1


def test_kind_scoping_keeps_the_channels_independent(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.daemon, "hierarchy_tail_seal_enabled", True)
    now = datetime.now(UTC)

    # 1. A clean compact finish must not pace the tail channel's interval: the
    #    tail channel has no history of its own yet, so the first seal is due.
    tail_agent = 880_120
    _state(db_conn, tail_agent, cid(1))
    _clean_compact_job(db_conn, tail_agent, cid(1), finished_minutes_ago=5)
    _plain_checkpoint(db_conn, tail_agent, now - timedelta(minutes=30), 9)
    assert scan(db_conn).tail_enqueued == 1

    # 2. A failed tail attempt must not stall the compact drain: the compact
    #    channel's continuation reads its own kind's history only.
    compact_agent = 880_121
    boundary = _boundary(db_conn, compact_agent, 1)
    _state(db_conn, compact_agent, boundary)
    db_conn.execute(
        "INSERT INTO hierarchy_jobs (agent_id, kind, trigger_boundary, status, include_tail,"
        " failed, skipped, finished_at)"
        " VALUES (%s, 'compact', %s, 'done', true, 0, 3, now())",
        (compact_agent, boundary),
    )
    db_conn.execute(
        "INSERT INTO hierarchy_jobs (agent_id, kind, trigger_boundary, status, include_tail,"
        " error, finished_at)"
        " VALUES (%s, 'tail', %s, 'failed', true, 'boom', now())",
        (compact_agent, boundary),
    )
    db_conn.commit()
    outcome = scan(db_conn)
    assert outcome.enqueued == 1  # the continuum, not stalled by the tail failure
    assert outcome.tail_enqueued == 0


def test_execute_tail_records_the_delta_column_and_leaves_the_cursor(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_id = 880_130
    boundary = cid(1)
    _state(db_conn, agent_id, boundary)
    newest = _plain_checkpoint(db_conn, agent_id, datetime.now(UTC) - timedelta(minutes=30), 9)
    row = db_conn.execute(
        "INSERT INTO hierarchy_jobs (agent_id, kind, trigger_boundary, status, include_tail)"
        " VALUES (%s, 'tail', %s, 'running', true) RETURNING id",
        (agent_id, cid(2)),
    ).fetchone()
    assert row is not None
    job_id = int(row[0])
    db_conn.commit()

    complete = MaterializedTree(
        nodes=(), errors=(), pending={}, max_level=1, batches=2, generated=1, skipped=0
    )

    def fake_known(*args: object, **kwargs: object) -> dict[str, str]:
        return {}

    def fake_tree(*args: object, **kwargs: object) -> MaterializedTree:
        return complete

    def fake_write(*args: object, **kwargs: object) -> int:
        return 0

    monkeypatch.setattr(execute_module, "load_known_texts", fake_known)
    monkeypatch.setattr(execute_module, "build_agent_tree", fake_tree)
    monkeypatch.setattr(execute_module, "write_tree", fake_write)

    assert execute_job(job_id) == 0
    state = db_conn.execute(
        "SELECT last_processed_boundary, last_tail_seal_cp_id FROM hierarchy_worker_state"
        " WHERE agent_id = %s",
        (agent_id,),
    ).fetchone()
    # The tail run records its sealed stretch and leaves the compact cursor.
    assert state == (boundary, newest)


def test_execute_tail_leaves_the_delta_column_unset_when_truncated(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_id = 880_131
    _state(db_conn, agent_id, cid(1))
    _plain_checkpoint(db_conn, agent_id, datetime.now(UTC) - timedelta(minutes=30), 9)
    row = db_conn.execute(
        "INSERT INTO hierarchy_jobs (agent_id, kind, trigger_boundary, status, include_tail)"
        " VALUES (%s, 'tail', %s, 'running', true) RETURNING id",
        (agent_id, cid(2)),
    ).fetchone()
    assert row is not None
    job_id = int(row[0])
    db_conn.commit()

    truncated = MaterializedTree(
        nodes=(), errors=(), pending={}, max_level=1, batches=2, generated=1, skipped=4
    )

    def fake_known(*args: object, **kwargs: object) -> dict[str, str]:
        return {}

    def fake_tree(*args: object, **kwargs: object) -> MaterializedTree:
        return truncated

    def fake_write(*args: object, **kwargs: object) -> int:
        return 0

    monkeypatch.setattr(execute_module, "load_known_texts", fake_known)
    monkeypatch.setattr(execute_module, "build_agent_tree", fake_tree)
    monkeypatch.setattr(execute_module, "write_tree", fake_write)

    assert execute_job(job_id) == 0
    row_out = db_conn.execute(
        "SELECT status, skipped FROM hierarchy_jobs WHERE id = %s", (job_id,)
    ).fetchone()
    assert row_out == ("done", 4)
    state = db_conn.execute(
        "SELECT last_tail_seal_cp_id FROM hierarchy_worker_state WHERE agent_id = %s",
        (agent_id,),
    ).fetchone()
    assert state == (None,)  # not fully sealed: the next pass resumes it
