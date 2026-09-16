"""`services.hierarchy_worker` — the compact-driven build queue (task #3704 P2b).

Exercised against the session's real Postgres. Contracts pinned here: the
silent baseline (no build for pre-existing history), enqueue-on-new-boundary
with first-build tail semantics, the live-job de-dup, the retry backoff vs
immediate continuation drain, stale-running recovery, the atomic claim, the
child side's row outcome (scope + cursor advance only on a complete run), and
one real child-process round trip over an agent with no checkpoint history
(zero model calls — the empty-history edge is the fixture).
"""

from __future__ import annotations

import psycopg
import pytest

from services.hierarchy_worker import execute as execute_module
from services.hierarchy_worker import runner
from services.hierarchy_worker.scan import scan
from shared.config import settings
from shared.hierarchy.pipeline import MaterializedTree


# Lexicographically ordered UUIDv6-shaped checkpoint ids, one per `nth`.
def cid(nth: int) -> str:
    return f"1f1b2202-0000-6000-a3e4-{nth:012x}"


def _boundary(conn: psycopg.Connection, agent_id: int, nth: int) -> str:
    """Insert one compact-boundary checkpoint row for `agent_id`."""
    checkpoint_id = cid(nth)
    conn.execute(
        "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, checkpoint, metadata)"
        " VALUES (%s, '', %s, '{}'::jsonb, '{\"compact_boundary\": true}'::jsonb)",
        (str(agent_id), checkpoint_id),
    )
    conn.commit()
    return checkpoint_id


def _state(conn: psycopg.Connection, agent_id: int, boundary: str) -> None:
    conn.execute(
        "INSERT INTO hierarchy_worker_state (agent_id, last_processed_boundary) VALUES (%s, %s)",
        (agent_id, boundary),
    )
    conn.commit()


def _job_row(conn: psycopg.Connection, job_id: int) -> tuple[str, int, int]:
    row = conn.execute(
        "SELECT status, coalesce(failed, 0), coalesce(skipped, 0)"
        " FROM hierarchy_jobs WHERE id = %s",
        (job_id,),
    ).fetchone()
    assert row is not None
    return str(row[0]), int(row[1]), int(row[2])


def test_first_sight_baselines_silently(db_conn: psycopg.Connection) -> None:
    _boundary(db_conn, 880_001, 3)
    outcome = scan(db_conn)
    assert outcome.baselined == 1
    assert outcome.enqueued == 0
    row = db_conn.execute(
        "SELECT last_processed_boundary FROM hierarchy_worker_state WHERE agent_id = 880001"
    ).fetchone()
    assert row is not None and row[0] == cid(3)
    count_row = db_conn.execute("SELECT count(*) FROM hierarchy_jobs").fetchone()
    assert count_row is not None and count_row[0] == 0


def test_new_boundary_enqueues_a_first_build_once(db_conn: psycopg.Connection) -> None:
    _boundary(db_conn, 880_002, 1)
    scan(db_conn)  # silent baseline
    _boundary(db_conn, 880_002, 2)

    assert scan(db_conn).enqueued == 1
    row = db_conn.execute(
        "SELECT trigger_boundary, include_tail, status FROM hierarchy_jobs WHERE agent_id = 880002"
    ).fetchone()
    assert row is not None
    assert row[0] == cid(2)
    assert row[1] is True  # the first build seals the tail (manual-first-run semantics)
    assert row[2] == "pending"

    # A live (pending/running) job de-dups further enqueues.
    assert scan(db_conn).enqueued == 0


def test_include_tail_turns_false_after_a_clean_completion(db_conn: psycopg.Connection) -> None:
    _boundary(db_conn, 880_003, 1)
    scan(db_conn)
    _boundary(db_conn, 880_003, 2)
    scan(db_conn)
    db_conn.execute(
        "UPDATE hierarchy_jobs SET status = 'done', finished_at = now(), failed = 0, skipped = 0"
        " WHERE agent_id = 880003"
    )
    db_conn.execute(
        "UPDATE hierarchy_worker_state SET last_processed_boundary = %s WHERE agent_id = 880003",
        (cid(2),),
    )
    db_conn.commit()

    _boundary(db_conn, 880_003, 3)
    assert scan(db_conn).enqueued == 1
    row = db_conn.execute(
        "SELECT trigger_boundary, include_tail FROM hierarchy_jobs"
        " WHERE agent_id = 880003 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None and row[0] == cid(3) and row[1] is False


def test_clean_completion_with_skips_keeps_the_first_build_mode(
    db_conn: psycopg.Connection,
) -> None:
    """A truncated (skipped>0) attempt is not a clean completion: the next
    attempt is still the first full build and still seals the tail."""
    _boundary(db_conn, 880_004, 1)
    scan(db_conn)
    _boundary(db_conn, 880_004, 2)
    scan(db_conn)
    db_conn.execute(
        "UPDATE hierarchy_jobs SET status = 'done', finished_at = now(), failed = 0, skipped = 5"
        " WHERE agent_id = 880004"
    )
    db_conn.commit()

    assert scan(db_conn).enqueued == 1  # the continuation is due immediately
    row = db_conn.execute(
        "SELECT include_tail FROM hierarchy_jobs WHERE agent_id = 880004 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None and row[0] is True


def test_backoff_gates_nonclean_retries_but_not_continuations(
    db_conn: psycopg.Connection,
) -> None:
    _boundary(db_conn, 880_005, 1)
    scan(db_conn)
    _boundary(db_conn, 880_005, 2)
    db_conn.execute(
        "INSERT INTO hierarchy_jobs (agent_id, kind, trigger_boundary, status, include_tail,"
        " error, finished_at)"
        " VALUES (880005, 'compact', %s, 'failed', true, 'boom', now())",
        (cid(2),),
    )
    db_conn.commit()

    assert scan(db_conn).enqueued == 0  # inside the backoff window

    db_conn.execute(
        "UPDATE hierarchy_jobs SET finished_at = now() - interval '2 hours' WHERE agent_id = 880005"
    )
    db_conn.commit()
    assert scan(db_conn).enqueued == 1  # past the base backoff

    # A pure continuation (done, no failures, skipped>0) drains immediately.
    db_conn.execute(
        "UPDATE hierarchy_jobs SET status = 'done', error = NULL, failed = 0, skipped = 3,"
        " finished_at = now() WHERE agent_id = 880005"
    )
    db_conn.commit()
    assert scan(db_conn).enqueued == 1


def test_stale_running_rows_are_recovered(db_conn: psycopg.Connection) -> None:
    _boundary(db_conn, 880_006, 1)
    scan(db_conn)
    deadline_s = settings.daemon.hierarchy_job_deadline_seconds
    db_conn.execute(
        "INSERT INTO hierarchy_jobs (agent_id, kind, trigger_boundary, status, include_tail,"
        " started_at)"
        " VALUES (880006, 'compact', %s, 'running', true, now() - make_interval(secs => %s))",
        (cid(1), deadline_s + 600),
    )
    db_conn.execute(
        "INSERT INTO hierarchy_jobs (agent_id, kind, trigger_boundary, status, include_tail,"
        " started_at)"
        " VALUES (880007, 'compact', %s, 'running', true, now())",
        (cid(1),),
    )
    db_conn.commit()

    outcome = scan(db_conn)
    assert outcome.stale_recovered == 1
    stale = db_conn.execute(
        "SELECT status, error FROM hierarchy_jobs WHERE agent_id = 880006"
    ).fetchone()
    fresh = db_conn.execute("SELECT status FROM hierarchy_jobs WHERE agent_id = 880007").fetchone()
    assert stale is not None and stale[0] == "failed" and "stale" in str(stale[1])
    assert fresh is not None and fresh[0] == "running"


def test_claim_next_is_atomic_and_fifo(db_conn: psycopg.Connection) -> None:
    for agent in (880_010, 880_011):
        db_conn.execute(
            "INSERT INTO hierarchy_jobs (agent_id, kind, trigger_boundary, status, include_tail)"
            " VALUES (%s, 'compact', %s, 'pending', true)",
            (agent, cid(1)),
        )
    db_conn.commit()

    first = runner.claim_next(db_conn)
    second = runner.claim_next(db_conn)
    assert first is not None and second is not None
    assert first.agent_id == 880_010 and second.agent_id == 880_011
    assert runner.claim_next(db_conn) is None
    row = db_conn.execute(
        "SELECT status, started_at FROM hierarchy_jobs WHERE id = %s", (first.id,)
    ).fetchone()
    assert row is not None and row[0] == "running" and row[1] is not None


def _make_running_job(db_conn: psycopg.Connection, agent_id: int, boundary: str) -> int:
    row = db_conn.execute(
        "INSERT INTO hierarchy_jobs (agent_id, kind, trigger_boundary, status, include_tail)"
        " VALUES (%s, 'compact', %s, 'running', false) RETURNING id",
        (agent_id, boundary),
    ).fetchone()
    assert row is not None
    db_conn.commit()
    return int(row[0])


def test_execute_records_scope_and_advances_only_when_complete(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_id = 880_020
    _boundary(db_conn, agent_id, 3)
    _state(db_conn, agent_id, cid(1))
    job_id = _make_running_job(db_conn, agent_id, cid(2))

    complete = MaterializedTree(
        nodes=(),
        errors=(),
        pending={},
        max_level=1,
        batches=4,
        generated=6,
        reused=2,
        skipped=0,
        src_tokens=1000,
        out_tokens=100,
    )

    def fake_known(*args: object, **kwargs: object) -> dict[str, str]:
        return {}

    def fake_tree(*args: object, **kwargs: object) -> MaterializedTree:
        return complete

    def fake_write(*args: object, **kwargs: object) -> int:
        return 8

    monkeypatch.setattr(execute_module, "load_known_texts", fake_known)
    monkeypatch.setattr(execute_module, "build_agent_tree", fake_tree)
    monkeypatch.setattr(execute_module, "write_tree", fake_write)

    assert execute_module.execute_job(job_id) == 0
    row = db_conn.execute(
        "SELECT status, stretches, nodes, generated, reused, failed, skipped, src_tokens,"
        " out_tokens FROM hierarchy_jobs WHERE id = %s",
        (job_id,),
    ).fetchone()
    assert row is not None
    assert row == ("done", 4, 8, 6, 2, 0, 0, 1000, 100)
    state = db_conn.execute(
        "SELECT last_processed_boundary FROM hierarchy_worker_state WHERE agent_id = %s",
        (agent_id,),
    ).fetchone()
    # Advance target = the newest boundary read before loading (cid(3)).
    assert state is not None and state[0] == cid(3)


def test_execute_does_not_advance_the_cursor_on_a_truncated_run(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_id = 880_021
    _boundary(db_conn, agent_id, 3)
    _state(db_conn, agent_id, cid(1))
    job_id = _make_running_job(db_conn, agent_id, cid(2))

    truncated = MaterializedTree(
        nodes=(), errors=(), pending={}, max_level=1, batches=4, generated=1, skipped=9
    )

    def fake_known(*args: object, **kwargs: object) -> dict[str, str]:
        return {}

    def fake_tree(*args: object, **kwargs: object) -> MaterializedTree:
        return truncated

    def fake_write(*args: object, **kwargs: object) -> int:
        return 1

    monkeypatch.setattr(execute_module, "load_known_texts", fake_known)
    monkeypatch.setattr(execute_module, "build_agent_tree", fake_tree)
    monkeypatch.setattr(execute_module, "write_tree", fake_write)

    assert execute_module.execute_job(job_id) == 0
    status, failed, skipped = _job_row(db_conn, job_id)
    assert (status, failed, skipped) == ("done", 0, 9)
    state = db_conn.execute(
        "SELECT last_processed_boundary FROM hierarchy_worker_state WHERE agent_id = %s",
        (agent_id,),
    ).fetchone()
    assert state is not None and state[0] == cid(1)  # unchanged — not complete


def test_execute_records_failures_on_the_row(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_id = 880_022
    _state(db_conn, agent_id, cid(1))
    job_id = _make_running_job(db_conn, agent_id, cid(1))

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("kaput")

    monkeypatch.setattr(execute_module, "build_agent_tree", boom)
    assert execute_module.execute_job(job_id) == 1
    row = db_conn.execute(
        "SELECT status, error FROM hierarchy_jobs WHERE id = %s", (job_id,)
    ).fetchone()
    assert row is not None and row[0] == "failed" and "kaput" in str(row[1])


def test_child_round_trip_on_empty_history(db_conn: psycopg.Connection) -> None:
    """A full claim -> child -> done round trip through the real subprocess.

    The agent has no checkpoint history, so the build walks an empty tree:
    zero model calls, and the clean run still advances the cursor."""
    agent_id = 880_030
    _state(db_conn, agent_id, cid(1))
    row = db_conn.execute(
        "INSERT INTO hierarchy_jobs (agent_id, kind, trigger_boundary, status, include_tail)"
        " VALUES (%s, 'compact', %s, 'pending', false) RETURNING id",
        (agent_id, cid(1)),
    ).fetchone()
    assert row is not None
    job_id = int(row[0])
    db_conn.commit()

    claimed = runner.claim_next(db_conn)
    assert claimed is not None and claimed.id == job_id
    # The worker claims on an autocommit connection; commit so the child
    # (a separate connection) sees the row as `running`.
    db_conn.commit()
    runner.run_child(claimed)

    row = db_conn.execute(
        "SELECT status, coalesce(failed, 0), coalesce(skipped, 0) FROM hierarchy_jobs"
        " WHERE id = %s",
        (job_id,),
    ).fetchone()
    assert row == ("done", 0, 0)
    state = db_conn.execute(
        "SELECT last_processed_boundary FROM hierarchy_worker_state WHERE agent_id = %s",
        (agent_id,),
    ).fetchone()
    assert state is not None and state[0] == cid(1)
