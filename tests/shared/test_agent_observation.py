"""Read existing observation clocks without inventing runtime ownership."""

from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from shared.agent_observation import LIVENESS_PASS_INTERVAL_S, MACHINE_OFFLINE_AFTER_FAILURES
from shared.agent_snapshot import AgentListSummary, select_all, select_one
from tests.conftest import spawn_agent


@pytest.mark.parametrize("probe_age", [None, 0, 600])
@pytest.mark.parametrize("lease_offset", [None, -60, 60])
def test_snapshot_retains_independent_probe_and_lease_clocks(
    db_conn: psycopg.Connection, probe_age: int | None, lease_offset: int | None
) -> None:
    aid = spawn_agent(spawner="user")
    now = datetime.now(UTC)
    probe = None if probe_age is None else now - timedelta(seconds=probe_age)
    lease = None if lease_offset is None else now + timedelta(seconds=lease_offset)
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET machine=%s, lease_expires_at=%s WHERE id=%s",
            ("observation-test", lease, aid),
        )
        if probe is not None:
            cur.execute(
                "INSERT INTO machine_probe(machine_name,online,consecutive_failures,last_probe_at) "
                "VALUES(%s,true,0,%s) ON CONFLICT(machine_name) DO UPDATE SET last_probe_at=EXCLUDED.last_probe_at",
                ("observation-test", probe),
            )
    db_conn.commit()
    full = select_one(db_conn, aid)
    assert full is not None and full.observation is not None
    evidence = full.observation
    assert evidence.runtime_owner == "unknown"
    assert evidence.runtime_lease_expires_at == lease
    assert evidence.machine_probe_at == probe
    assert evidence.machine_probe_valid_until == (
        None
        if probe is None
        else probe + timedelta(seconds=LIVENESS_PASS_INTERVAL_S * MACHINE_OFFLINE_AFTER_FAILURES)
    )
    summary = next(row for row in select_all(db_conn, fields="summary") if row.agent_id == aid)
    assert isinstance(summary, AgentListSummary)
    assert summary.observation == evidence
