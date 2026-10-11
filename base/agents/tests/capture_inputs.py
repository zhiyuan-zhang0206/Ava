"""Explicit lease roots and event-log observations shared by capture contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from base.agents.impersonation import history as history
from base.agents.impersonation.manifest import (
    LocalParticipant,
    open_local_participant,
)
from base.agents.impersonation.tests import test_history as history_cases
from base.cluster.machine import MachineIdentity
from base.config.service_read import ConfigAuthority
from base.db import Database, create_agent
from base.db.code_version_gate import ProcessDbGate
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.telemetry import Event
from tests.fixtures.unit.identity import machine_identity as machine_identity

__all__ = [
    "capture_rows",
    "capture_state",
    "create_capture_owner",
    "lease",
    "open_capture",
    "owner",
    "sdk_event",
]


def create_capture_owner(
    db_conn: psycopg.Connection[Any], *, local_machine: str
) -> RuntimeIncarnation:
    agent_id = create_agent(db_conn)
    incarnation = RuntimeIncarnation(agent_id, uuid4(), uuid4())
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine,runtime_generation,runtime_owner,"
        "runtime_kind,lease_expires_at) VALUES(%s,'idling',%s,%s,%s,'process',"
        "clock_timestamp()+interval '10 minutes')",
        (agent_id, local_machine, incarnation.generation, incarnation.owner),
    )
    db_conn.commit()
    return incarnation


@pytest.fixture
def owner(
    db_conn: psycopg.Connection[Any], *, machine_identity: MachineIdentity
) -> RuntimeIncarnation:
    local_machine = machine_identity.name()
    return create_capture_owner(db_conn, local_machine=local_machine)


@pytest.fixture
def lease(
    owner: RuntimeIncarnation,
    *,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> dict[str, Any]:
    local_machine = machine_identity.name()
    return history_cases.start(
        owner, authority=config_authority, database_gate=database_gate, local_machine=local_machine
    )


def sdk_event(agent_id: int, marker: str) -> Event:
    return Event(
        ts=datetime.now(UTC),
        trace_id=None,
        span_id=None,
        agent_id=agent_id,
        machine="test-machine",
        cluster="test-cluster",
        process="test",
        category="telemetry",
        event_name="sdk_call",
        level="info",
        source=f"agent:{agent_id}",
        target_agent_id=None,
        attributes={"fn": marker, "duration": 0.1},
    )


def open_capture(
    lease: dict[str, Any], agent_id: int, key: str, database_gate: ProcessDbGate
) -> LocalParticipant:
    participant = LocalParticipant(
        str(lease["id"]),
        agent_id,
        lease["session_id"],
        key,
        Database.from_settings(gate=database_gate),
    )
    assert open_local_participant(
        Database.from_settings(gate=database_gate),
        participant.lease_id,
        agent_id=agent_id,
        source_key=key,
    )
    return participant


def capture_state(
    db_conn: psycopg.Connection[Any], participant: LocalParticipant
) -> tuple[Any, ...]:
    row = db_conn.execute(
        "SELECT state FROM agent_impersonation_event_participants "
        "WHERE lease_id=%s AND source_key=%s",
        (participant.lease_id, participant.source_key),
    ).fetchone()
    assert row is not None
    return row


def capture_rows(db_conn: psycopg.Connection[Any], lease_id: object, source_key: str) -> int:
    row = db_conn.execute(
        "SELECT count(*) FROM agent_impersonation_entries WHERE lease_id=%s AND source_key=%s",
        (lease_id, source_key),
    ).fetchone()
    assert row is not None
    return int(row[0])
