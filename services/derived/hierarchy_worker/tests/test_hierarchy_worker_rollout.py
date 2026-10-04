"""The rollout allowlist (`hierarchy_worker_agents`): the worker serves only the listed agents.

The scan baselines, follows and tail-seals only listed agents and the claim leaves an unlisted
agent's pending job parked; an empty list serves everyone. The agent-side enqueue is pinned in
`base/agents/history/tests/test_checkpoint_cleanup.py`.
"""

from __future__ import annotations

import psycopg
import pytest

from base.config.domains.daemon.hierarchy_worker_fields import parse_hierarchy_worker_agents
from services.derived.hierarchy_worker import runner
from services.derived.hierarchy_worker import scan as scan_module
from services.derived.hierarchy_worker.tests.slices import hierarchy_config


def cid(nth: int) -> str:
    """Lexicographically ordered UUIDv6-shaped checkpoint ids, one per `nth`."""
    return f"1f1b2202-0000-6000-a3e4-{nth:012x}"


def _boundary(conn: psycopg.Connection, agent_id: int, nth: int) -> None:
    conn.execute(
        "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, checkpoint, metadata)"
        " VALUES (%s, '', %s, '{}'::jsonb, '{\"compact_boundary\": true}'::jsonb)",
        (str(agent_id), cid(nth)),
    )
    conn.commit()


def _pending(conn: psycopg.Connection, agent_id: int) -> int:
    row = conn.execute(
        "INSERT INTO hierarchy_jobs (agent_id, kind, trigger_boundary, status, include_tail)"
        " VALUES (%s, 'compact', %s, 'pending', false) RETURNING id",
        (agent_id, cid(1)),
    ).fetchone()
    assert row is not None
    conn.commit()
    return int(row[0])


def test_parse_accepts_ids_and_rejects_everything_else() -> None:
    assert parse_hierarchy_worker_agents("") == frozenset()
    assert parse_hierarchy_worker_agents(" 7, 12 ,,7") == frozenset({7, 12})
    for bad in ("x", "0", "-3", "1.5", "1;2"):
        with pytest.raises(ValueError, match="positive agent id"):
            parse_hierarchy_worker_agents(bad)


def test_scan_baselines_only_listed_agents(db_conn: psycopg.Connection) -> None:
    _boundary(db_conn, 880_501, 1)
    _boundary(db_conn, 880_502, 2)

    outcome = scan_module.scan(db_conn, hierarchy_config(hierarchy_worker_agents="880501"))

    assert outcome.baselined == 1
    tracked = db_conn.execute(
        "SELECT agent_id FROM hierarchy_worker_state WHERE agent_id IN (880501, 880502)"
    ).fetchall()
    assert tracked == [(880_501,)]


def test_scan_follows_a_listed_agent_and_skips_an_unlisted_one(db_conn: psycopg.Connection) -> None:
    for agent_id in (880_511, 880_512):
        _boundary(db_conn, agent_id, 1)
        db_conn.execute(
            "INSERT INTO hierarchy_worker_state (agent_id, last_processed_boundary)"
            " VALUES (%s, %s)",
            (agent_id, cid(1)),
        )
        _boundary(db_conn, agent_id, 2)  # a newer boundary: both agents are behind
    db_conn.commit()

    outcome = scan_module.scan(db_conn, hierarchy_config(hierarchy_worker_agents="880512"))

    assert outcome.enqueued == 1
    jobs = db_conn.execute(
        "SELECT agent_id FROM hierarchy_jobs WHERE agent_id IN (880511, 880512)"
    ).fetchall()
    assert jobs == [(880_512,)]


def test_claim_leaves_an_unlisted_agents_job_parked(db_conn: psycopg.Connection) -> None:
    for agent_id in (880_521, 880_522):
        db_conn.execute(
            "INSERT INTO hierarchy_worker_state (agent_id, last_processed_boundary)"
            " VALUES (%s, %s)",
            (agent_id, cid(1)),
        )
    db_conn.commit()
    parked = _pending(db_conn, 880_521)
    wanted = _pending(db_conn, 880_522)

    claimed = runner.claim_next(db_conn, agents=frozenset({880_522}))

    assert claimed is not None and claimed.id == wanted
    assert runner.claim_next(db_conn, agents=frozenset({880_522})) is None
    status = db_conn.execute(
        "SELECT status FROM hierarchy_jobs WHERE id = %s", (parked,)
    ).fetchone()
    assert status == ("pending",)
    # An empty allowlist serves everyone: the parked job is claimable again.
    again = runner.claim_next(db_conn)
    assert again is not None and again.id == parked
