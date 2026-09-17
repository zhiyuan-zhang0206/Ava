"""`services.hierarchy_worker` — the compact-driven build queue (task #3704 P2b).

Exercised against the session's real Postgres. Contracts pinned here: the
silent baseline (no build for pre-existing history), enqueue-on-new-boundary
with first-build tail semantics, the live-job de-dup, the retry backoff vs
immediate continuation drain, stale-running recovery, the atomic claim, the
child side's row outcome (scope + cursor advance only on a complete run),
the tick's drain and crash mapping (a tick drains back-to-back; a transient
failure ends the tick; code<->DB drift raises), one real child-process
round trip over an agent with no checkpoint history (zero model calls — the
empty-history edge is the fixture), and the process-boot sink seam on both
sides of the worker (task #3868): the host's prepare() and the child's
job.main() open the log / event-pipeline sinks, so a real child subprocess's
records reach stderr and the events JSONL mirror.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest

from services.hierarchy_worker import execute as execute_module
from services.hierarchy_worker import job as job_module
from services.hierarchy_worker import runner
from services.hierarchy_worker.scan import ScanOutcome, scan
from shared.config import settings
from shared.hierarchy.pipeline import MaterializedTree
from shared.paths import logs_dir

REPO_ROOT = Path(__file__).resolve().parents[2]


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


def test_job_main_inits_the_child_process_sinks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`job.main` boots the child's process sinks before executing.

    `shared/log.py` drops loguru's default handler at import, so without the
    seam every record the child produces — the `llm_usage` rows that are the
    worker's metering ledger included — is silently discarded (task #3868)."""
    order: list[str] = []

    def fake_init(*, name: str) -> None:
        order.append(f"init:{name}")

    def fake_execute(job_id: int) -> int:
        order.append(f"execute:{job_id}")
        return 7

    monkeypatch.setattr(job_module, "init_gateway_process", fake_init)
    monkeypatch.setattr(execute_module, "execute_job", fake_execute)

    assert job_module.main(["--job-id", "77"]) == 7
    assert order == ["init:hierarchy-worker", "execute:77"]


def test_child_process_records_reach_the_event_mirror() -> None:
    """A real child subprocess boots its process sinks (task #3868).

    The job-vanished error reaches stderr, and the child's records — its boot
    row and the error line — land in the events JSONL mirror under process
    `hierarchy-worker`. Before the seam, the same probe exited 1 with zero
    output and zero records."""
    child = subprocess.run(
        [sys.executable, "-m", "services.hierarchy_worker.job", "--job-id", "999999"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert child.returncode == 1
    assert "vanished" in child.stderr

    mirror = logs_dir() / f"events-{datetime.now(UTC).strftime('%Y%m%d')}.jsonl"
    assert mirror.exists(), "the child wrote no events mirror — its sink seam did not boot"
    child_lines = [
        line
        for line in mirror.read_text(encoding="utf-8").splitlines()
        if '"process":"hierarchy-worker"' in line
    ]
    assert any('"event_name":"service_started"' in line for line in child_lines)
    assert any(
        '"event_name":"log"' in line and "vanished before execution" in line for line in child_lines
    )


class _FakeConnection:
    """Just enough of `connect(autocommit=True)`'s context manager for ticks."""

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _fake_connect(**_kw: object) -> _FakeConnection:
    return _FakeConnection()


def test_run_tick_drains_back_to_back_then_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tick re-scans, claims, and runs every due job before returning; the
    slot boundary paces only the idle wait between ticks."""
    scanned: list[int] = []
    ran: list[int] = []
    jobs = [
        runner.ClaimedJob(id=1, agent_id=11, include_tail=True),
        runner.ClaimedJob(id=2, agent_id=12, include_tail=False),
    ]

    def fake_scan(conn: object) -> ScanOutcome:
        scanned.append(len(scanned))
        return ScanOutcome()

    def fake_claim(conn: object) -> runner.ClaimedJob | None:
        return jobs.pop(0) if jobs else None

    monkeypatch.setattr(runner, "connect", _fake_connect)
    monkeypatch.setattr(runner, "scan", fake_scan)
    monkeypatch.setattr(runner, "claim_next", fake_claim)

    def fake_child(job: runner.ClaimedJob) -> None:
        ran.append(job.id)

    monkeypatch.setattr(runner, "run_child", fake_child)

    runner.run_tick()

    assert ran == [1, 2]
    # One scan per claim, plus the dry pass that ends the tick.
    assert len(scanned) == 3


def test_run_tick_returns_on_a_transient_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transient DB failure ends the tick without raising — the next slot
    retries, so the manager never sees a crash for a blip."""

    def failing_scan(conn: object) -> ScanOutcome:
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(runner, "connect", _fake_connect)
    monkeypatch.setattr(runner, "scan", failing_scan)

    runner.run_tick()  # returns — no exception escapes the tick


def test_run_tick_raises_on_schema_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    """Code<->DB drift escapes the tick so the manager's crash path records it."""

    def drifted_scan(conn: object) -> ScanOutcome:
        raise psycopg.ProgrammingError('relation "hierarchy_jobs" does not exist')

    monkeypatch.setattr(runner, "connect", _fake_connect)
    monkeypatch.setattr(runner, "scan", drifted_scan)

    with pytest.raises(psycopg.ProgrammingError):
        runner.run_tick()


def test_prepare_inits_the_host_process_sinks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The host's one-time start opens its process sinks before the schema
    check: the schedule runner process is otherwise sink-less, so the start /
    scan / claim lines and a drifted-schema crash would all be dropped
    records (task #3868)."""
    order: list[str] = []

    def fake_init(*, name: str) -> None:
        order.append(f"init:{name}")

    def fake_schema(db_url: str) -> None:
        order.append("schema")

    monkeypatch.setattr(runner, "init_gateway_process", fake_init)
    monkeypatch.setattr("shared.migrations.assert_schema_current", fake_schema)

    runner.prepare()
    assert order == ["init:schedule-hierarchy-worker", "schema"]
