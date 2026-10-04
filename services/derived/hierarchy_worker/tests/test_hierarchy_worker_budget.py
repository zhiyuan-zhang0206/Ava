"""First builds defer to the next day instead of tripping the breaker (task #4674 guardrails).

The fleet's first-build wave (every agent's one-time full-window build) is paced by its own
24h budget: past it the claim parks first-build jobs and takes everything else, and they
resume as earlier jobs age out of the window. The anomaly breaker counts only the rest.
"""

from __future__ import annotations

import psycopg
import pytest

from base.config import settings
from base.db.tests.fakes import fake_database
from services.derived.hierarchy_worker import runner
from services.derived.hierarchy_worker.tests.slices import hierarchy_config


def cid(nth: int) -> str:
    """Lexicographically ordered UUIDv6-shaped checkpoint ids, one per `nth`."""
    return f"1f1b2202-0000-6000-a3e4-{nth:012x}"


def _finished(
    conn: psycopg.Connection,
    agent_id: int,
    *,
    generated: int,
    include_tail: bool,
    kind: str = "compact",
    age_hours: int = 0,
) -> None:
    conn.execute(
        "INSERT INTO hierarchy_jobs (agent_id, kind, trigger_boundary, status, include_tail,"
        " generated, finished_at)"
        " VALUES (%s, %s, %s, 'done', %s, %s, now() - make_interval(hours => %s))",
        (agent_id, kind, cid(1), include_tail, generated, age_hours),
    )
    conn.commit()


def _tracked(conn: psycopg.Connection, agent_id: int) -> None:
    conn.execute(
        "INSERT INTO hierarchy_worker_state (agent_id, last_processed_boundary) VALUES (%s, %s)",
        (agent_id, cid(1)),
    )
    conn.commit()


def _pending(conn: psycopg.Connection, agent_id: int, kind: str = "compact") -> int:
    row = conn.execute(
        "INSERT INTO hierarchy_jobs (agent_id, kind, trigger_boundary, status, include_tail)"
        " VALUES (%s, %s, %s, 'pending', %s) RETURNING id",
        (agent_id, kind, cid(2), kind == "tail"),
    ).fetchone()
    assert row is not None
    conn.commit()
    return int(row[0])


def test_the_breaker_does_not_count_first_builds(db_conn: psycopg.Connection) -> None:
    config = hierarchy_config(hierarchy_regen_daily_budget_nodes=100)
    _finished(db_conn, 880_601, generated=5_000, include_tail=True)  # a first build: not counted
    assert runner._regen_budget_check(db_conn, config) is False

    _finished(db_conn, 880_602, generated=101, include_tail=False)  # an ordinary job: counted
    assert runner._regen_budget_check(db_conn, config) is True


def test_first_builds_defer_once_their_own_window_is_spent(db_conn: psycopg.Connection) -> None:
    config = hierarchy_config(hierarchy_first_build_daily_budget_nodes=100)
    defer = runner._first_builds_deferred
    _finished(db_conn, 880_611, generated=99, include_tail=True)
    assert defer(db_conn, config) is False

    _finished(db_conn, 880_612, generated=500, include_tail=False)  # ordinary rows never count
    _finished(db_conn, 880_613, generated=500, include_tail=True, kind="tail")  # nor tail seals
    _finished(db_conn, 880_614, generated=500, include_tail=True, age_hours=25)  # nor an old day
    assert defer(db_conn, config) is False

    _finished(db_conn, 880_615, generated=1, include_tail=True)
    assert defer(db_conn, config) is True  # spent: at the budget


def test_a_deferred_claim_parks_first_builds_and_takes_the_rest(
    db_conn: psycopg.Connection,
) -> None:
    first, established, untracked, tail = 880_621, 880_622, 880_623, 880_624
    for agent_id in (first, established, tail):
        _tracked(db_conn, agent_id)
    _finished(db_conn, established, generated=3, include_tail=False)
    db_conn.execute(
        "UPDATE hierarchy_jobs SET failed = 0, skipped = 0 WHERE agent_id = %s", (established,)
    )
    db_conn.commit()
    first_job = _pending(db_conn, first)  # tracked, no clean build: a first build
    _pending(db_conn, untracked)  # never tracked: baselines for free
    established_job = _pending(db_conn, established)  # has a clean build: ordinary
    # A tail seal needs an established baseline; it is never a first build.
    _finished(db_conn, tail, generated=3, include_tail=False)
    db_conn.execute(
        "UPDATE hierarchy_jobs SET failed = 0, skipped = 0 WHERE agent_id = %s", (tail,)
    )
    db_conn.commit()
    tail_job = _pending(db_conn, tail, kind="tail")

    claimed = [
        runner.claim_next(db_conn, defer_first_builds=True),
        runner.claim_next(db_conn, defer_first_builds=True),
    ]
    assert {job.id for job in claimed if job} == {established_job, tail_job}
    assert runner.claim_next(db_conn, defer_first_builds=True) is None  # only the parked one left
    parked = db_conn.execute(
        "SELECT status FROM hierarchy_jobs WHERE id = %s", (first_job,)
    ).fetchone()
    assert parked == ("pending",)
    baselined = db_conn.execute(
        "SELECT 1 FROM hierarchy_worker_state WHERE agent_id = %s", (untracked,)
    ).fetchone()
    assert baselined is not None  # the free first-sight baseline ran in passing

    # The next day the window has room: the parked first build is claimable, backfilled as one.
    resumed = runner.claim_next(db_conn, defer_first_builds=False)
    assert resumed is not None and resumed.id == first_job and resumed.include_tail is True


def test_the_tick_defers_first_builds_without_tripping_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[bool] = []

    class _Conn:
        def __enter__(self) -> _Conn:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

    def fake_claim(_conn: object, *, defer_first_builds: bool, **_kw: object) -> None:
        seen.append(defer_first_builds)

    monkeypatch.setattr(settings.daemon, "hierarchy_worker_enabled", True)
    monkeypatch.setattr(runner, "_fallback_scanned_at", None)

    def not_due(_now: object, _config: object) -> bool:
        return False

    def quiet(_conn: object, _config: object) -> bool:
        return False

    def spent(_conn: object, _config: object) -> bool:
        return True

    monkeypatch.setattr(runner, "_fallback_scan_due", not_due)
    monkeypatch.setattr(runner, "_regen_budget_check", quiet)
    monkeypatch.setattr(runner, "_first_builds_deferred", spent)
    monkeypatch.setattr(runner, "claim_next", fake_claim)

    def connect(**_kw: object) -> _Conn:
        return _Conn()

    runner.run_tick(hierarchy_config(), fake_database(connect))

    assert seen == [True]  # the drain asked the claim to defer first builds
