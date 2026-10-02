"""The runner's event-store sweep never touches log-native leases."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import psycopg
import pytest

from base.agents import impersonation as leases
from base.agents.impersonation import history as history
from base.agents.impersonation_manifest import open_local_participant
from base.cluster.machine import machine_name
from base.db import create_agent
from base.native_process.runtime_incarnation import RuntimeIncarnation
from services.agent_host import impersonation_events
from tests.base import test_history as history_cases
from tests.impersonation_support import attested_caller


def test_log_native_leases_are_never_swept_from_the_event_store(
    db_conn: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_id = create_agent(db_conn)
    owner = RuntimeIncarnation(agent_id, uuid4(), uuid4())
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine,runtime_generation,runtime_owner,"
        "runtime_kind,lease_expires_at) VALUES(%s,'idling',%s,%s,%s,'process',"
        "clock_timestamp()+interval '10 minutes')",
        (agent_id, machine_name(), owner.generation, owner.owner),
    )
    db_conn.commit()
    lease = history_cases.start(owner)
    assert lease["event_delivery_protocol_version"] == 2
    # An open source keeps the ended lease incomplete, so only the filter excludes it.
    assert open_local_participant(str(lease["id"]), agent_id=agent_id, source_key="unswept")
    db_conn.execute(
        "UPDATE agent_impersonations SET expires_at=clock_timestamp()-interval '1 second' "
        "WHERE id=%s",
        (lease["id"],),
    )
    db_conn.commit()
    assert leases.get(str(lease["id"]), attested_caller(lease))["status"] == "expired"
    assert history.resolve(agent_id, 0)["events_completed_at"] is None

    swept: list[dict[str, Any]] = []

    def record(session: dict[str, Any], **_: Any) -> None:
        swept.append(session)

    monkeypatch.setattr(impersonation_events, "consume_recorded_events", record)
    impersonation_events.reconcile_one()
    assert swept == []
