"""The hierarchy_jobs supersede grant's born-before coverage (task #4975).

`test_runner_role.py` exercises the runner's hierarchy_jobs INSERT in its grant
matrix (with the UPDATE beside it); the enqueue's conflict supersede needed the
UPDATE half to reach a cluster born before the entry, which only the start-path
refresh (`ensure_groups`) delivers. This test lives beside that file rather than
inside it because the matrix file sits at its frozen line-budget baseline.
"""

from __future__ import annotations

import psycopg
import pytest

from tests.base.test_runner_role import _grant_runner, _runner_url
from tests.base.test_runner_role import runner_db as runner_db


def test_hierarchy_jobs_supersede_grant_reaches_a_cluster_born_before_the_entry(
    runner_db: str,
) -> None:
    """Task #4975 regression: a cluster whose runner surface predates the
    hierarchy_jobs UPDATE entry keeps the INSERT only — the enqueue's conflict
    supersede fails with InsufficientPrivilege until the start-path refresh
    re-runs the grant layer, leaving a stop-left running row swallowing
    boundaries silently on the way."""
    _grant_runner(runner_db)
    with psycopg.connect(runner_db, autocommit=True) as conn:
        conn.execute("REVOKE UPDATE ON hierarchy_jobs FROM ava_runner")
        conn.execute(
            "INSERT INTO hierarchy_jobs (agent_id, kind, trigger_boundary, status, include_tail)"
            " VALUES (880_041, 'compact', 'b1', 'running', false)"
        )

    with (
        psycopg.connect(_runner_url(runner_db), autocommit=True) as conn,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        conn.execute(
            "UPDATE hierarchy_jobs SET status = 'failed'"
            " WHERE agent_id = 880_041 AND status = 'running'"
        )

    _grant_runner(runner_db)
    with psycopg.connect(_runner_url(runner_db), autocommit=True) as conn:
        conn.execute(
            "UPDATE hierarchy_jobs SET status = 'failed'"
            " WHERE agent_id = 880_041 AND status = 'running'"
        )
        row = conn.execute("SELECT status FROM hierarchy_jobs WHERE agent_id = 880_041").fetchone()
        assert row == ("failed",)
