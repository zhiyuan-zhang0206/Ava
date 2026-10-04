"""The stop-restart orphan reclaim (task #4975): the holder reap + pacing.

A stop kills the worker mid-job: the row stays `running` with a dead holder
pid, and the worker's next tick reaps it with the orphan marker before the
scan — that same pass re-enqueues the agent's newest boundary, with no
deadline + grace wait and no retry backoff. A crash that leaves the child
alive is deliberately not reaped: the holder pid is the child's own. The
scan's stale sweep stays the no-restart fallback (`test_hierarchy_worker.py`
pins it); the enqueue-side supersede half lives in
`base/agents/history/tests/test_checkpoint_cleanup.py`.
"""

from __future__ import annotations

import os
import subprocess
import sys

import psycopg

from base.agents.history.hierarchy.jobs import ORPHAN_MARKER
from services.hierarchy_worker import runner
from services.hierarchy_worker import scan as scan_module
from services.hierarchy_worker.scan import KIND_COMPACT
from services.hierarchy_worker.tests.slices import scan


# Lexicographically ordered UUIDv6-shaped checkpoint ids, one per `nth` — the
# same locals the sibling worker tests carry.
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


def _dead_pid() -> int:
    """A pid that belonged to a process and no longer does: the killed child."""
    holder = subprocess.Popen([sys.executable, "-c", "pass"])
    pid = holder.pid
    holder.wait()
    return pid


def _running_row(
    conn: psycopg.Connection, agent_id: int, boundary: str, holder_pid: int | None
) -> int:
    """One `running` job row as a killed (dead pid) or live child leaves it."""
    row = conn.execute(
        "INSERT INTO hierarchy_jobs (agent_id, kind, trigger_boundary, status, include_tail,"
        " started_at, holder_pid) VALUES (%s, 'compact', %s, 'running', true, now(), %s)"
        " RETURNING id",
        (agent_id, boundary, holder_pid),
    ).fetchone()
    assert row is not None
    conn.commit()
    return int(row[0])


def test_the_reap_fails_a_stop_killed_row_and_the_scan_requeues_immediately(
    db_conn: psycopg.Connection,
) -> None:
    """The stop-restart shape (task #4975): the killed job's holder pid is
    dead, its agent compacted again while it was dead (that enqueue met the
    row), and the worker restarts. The reap fails the row at once — inside its
    deadline window, where the stale sweep would have said nothing — and the
    same tick's scan re-enqueues the newest boundary: no deadline+grace wait,
    and no 1800s retry backoff (a reaped run is not an attempt outcome)."""
    agent_id = 880_050
    _boundary(db_conn, agent_id, 1)
    _state(db_conn, agent_id, cid(1))
    killed = _running_row(db_conn, agent_id, cid(1), _dead_pid())
    _boundary(db_conn, agent_id, 2)  # landed while the run was dead

    assert runner.reap_orphans(db_conn) == 1
    row = db_conn.execute(
        "SELECT status, error FROM hierarchy_jobs WHERE id = %s", (killed,)
    ).fetchone()
    assert row is not None and row[0] == "failed" and row[1] == ORPHAN_MARKER

    outcome = scan(db_conn)
    assert outcome.stale_recovered == 0  # inside the window: the reap did it, not the sweep
    assert outcome.enqueued == 1  # immediate — the reaped row gates nothing
    requeued = db_conn.execute(
        "SELECT status, trigger_boundary FROM hierarchy_jobs WHERE agent_id = %s"
        " ORDER BY id DESC LIMIT 1",
        (agent_id,),
    ).fetchone()
    assert requeued == ("pending", cid(2))


def test_the_reap_leaves_a_running_row_whose_holder_is_alive(
    db_conn: psycopg.Connection,
) -> None:
    """A crash that left the child alive is not a reap: the holder pid is the
    child's own, so it finishes and records its own outcome (task #4975)."""
    agent_id = 880_052
    job = _running_row(db_conn, agent_id, cid(1), os.getpid())  # a live holder

    assert runner.reap_orphans(db_conn) == 0
    row = db_conn.execute(
        "SELECT status, error FROM hierarchy_jobs WHERE id = %s", (job,)
    ).fetchone()
    assert row == ("running", None)


def test_the_reap_fails_a_row_whose_holder_never_registered(
    db_conn: psycopg.Connection,
) -> None:
    """No holder pid (the child died before it registered, or the row predates
    the column) is no holder: the row is reclaimed like a dead one."""
    agent_id = 880_053
    job = _running_row(db_conn, agent_id, cid(1), None)

    assert runner.reap_orphans(db_conn) == 1
    row = db_conn.execute(
        "SELECT status, error FROM hierarchy_jobs WHERE id = %s", (job,)
    ).fetchone()
    assert row is not None and row[0] == "failed" and row[1] == ORPHAN_MARKER


def test_orphan_marker_rows_are_invisible_to_the_pacing_reads(
    db_conn: psycopg.Connection,
) -> None:
    """A reaped run is not an attempt (task #4975): it is neither the last
    finished attempt (an earlier genuine failure keeps gating from its own
    timestamp) nor part of the non-clean streak (a real failure behind it
    keeps its own backoff step instead of being doubled)."""
    agent_id = 880_051
    _state(db_conn, agent_id, cid(1))
    db_conn.execute(
        "INSERT INTO hierarchy_jobs (agent_id, kind, trigger_boundary, status, include_tail,"
        " error, finished_at) VALUES (%s, 'compact', %s, 'failed', true, 'boom',"
        " now() - interval '2 hours')",
        (agent_id, cid(1)),
    )
    db_conn.execute(
        "INSERT INTO hierarchy_jobs (agent_id, kind, trigger_boundary, status, include_tail,"
        " error, finished_at) VALUES (%s, 'compact', %s, 'failed', true, %s, now())",
        (agent_id, cid(1), ORPHAN_MARKER),
    )
    db_conn.commit()

    last = scan_module._last_finished(db_conn, agent_id, KIND_COMPACT)
    assert last is not None and last.status == "failed" and last.error == "boom"
    assert scan_module._nonclean_streak(db_conn, agent_id, KIND_COMPACT) == 1
