"""Read existing observation clocks without inventing runtime ownership."""

from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from shared.agent_observation import (
    LIVENESS_PASS_INTERVAL_S,
    MACHINE_OFFLINE_AFTER_FAILURES,
    AvailabilityReason,
    availability,
)
from shared.agent_roster import AgentCard, select_roster
from shared.agent_snapshot import select_one
from shared.db import create_agent
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
    summary = next(row for row in select_roster(db_conn).agents if row.agent_id == aid)
    assert isinstance(summary, AgentCard)
    assert summary.observation == evidence


NOW = datetime(2026, 9, 24, 1, 0, tzinfo=UTC)


def _reason(
    *,
    host_online: bool | None,
    probe_age: timedelta | None = timedelta(0),
    admission_outcome: str | None = None,
    admission_age: timedelta | None = None,
) -> AvailabilityReason:
    observed = availability(
        status="idling",
        host_online=host_online,
        probe_at=NOW - probe_age if probe_age is not None else None,
        admission_outcome=admission_outcome,
        admission_at=NOW - admission_age if admission_age is not None else None,
        now=NOW,
    )
    assert observed.observed_at == NOW
    return observed.reason


def test_host_down_precedes_recent_admission_refusal() -> None:
    assert (
        _reason(
            host_online=False,
            admission_outcome="publication_deferred",
            admission_age=timedelta(seconds=10),
        )
        == AvailabilityReason.HOST_UNAVAILABLE
    )


def test_host_up_distinguishes_no_attempt_refusal_and_admission() -> None:
    assert _reason(host_online=True) == AvailabilityReason.AWAITING_ADMISSION
    assert (
        _reason(
            host_online=True,
            admission_outcome="resource_fence",
            admission_age=timedelta(seconds=10),
        )
        == AvailabilityReason.ADMISSION_REFUSED
    )
    assert (
        _reason(
            host_online=True,
            admission_outcome="admitted",
            admission_age=timedelta(seconds=10),
        )
        == AvailabilityReason.ADMITTED
    )


def test_stale_or_missing_probe_never_looks_ready() -> None:
    assert _reason(host_online=True, probe_age=timedelta(seconds=121)) == AvailabilityReason.UNKNOWN
    assert (
        _reason(host_online=False, probe_age=timedelta(seconds=121)) == AvailabilityReason.UNKNOWN
    )
    assert _reason(host_online=None) == AvailabilityReason.UNKNOWN
    assert _reason(host_online=True, probe_age=None) == AvailabilityReason.UNKNOWN


def test_stale_admission_outcome_is_unknown_even_with_live_host() -> None:
    assert (
        _reason(
            host_online=True,
            admission_outcome="publication_deferred",
            admission_age=timedelta(minutes=6),
        )
        == AvailabilityReason.UNKNOWN
    )


def test_snapshot_and_roster_share_machine_verdict(db_conn: psycopg.Connection) -> None:
    agent_id = create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agents_meta (id,status,machine) VALUES (%s,'idling','availability-host') "
        "ON CONFLICT (id) DO UPDATE SET status='idling', machine='availability-host'",
        (agent_id,),
    )
    db_conn.execute(
        "INSERT INTO machine_probe (machine_name,online,agent_host_online) "
        "VALUES ('availability-host',TRUE,FALSE)"
    )
    db_conn.commit()

    card = next(card for card in select_roster(db_conn).agents if card.agent_id == agent_id)
    detail = select_one(db_conn, agent_id)
    assert detail is not None
    assert card.status.value == detail.status.value == "idling"
    assert card.availability is not None and detail.availability is not None
    assert (
        card.availability.reason
        == detail.availability.reason
        == AvailabilityReason.HOST_UNAVAILABLE
    )
