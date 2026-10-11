"""Lease recovery preserves ownership, pending handoffs and expiry receipts."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import psycopg
import pytest

from base.agents import impersonation as leases
from base.agents.messages.caller_identity import CallerIdentity
from base.cluster.machine import MachineIdentity
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.events.live.tests.fakes import patch_announcements
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.tests.impersonation.support import (
    accept_controller,
    create_native_owner,
    native_status,
    request_controller,
)
from tests.fixtures.unit.identity import machine_identity as machine_identity
from tests.impersonation_support import attested_caller, recorded_tree


def test_termination_atomically_revokes_but_restart_preserves(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    local_machine = machine_identity.name()
    owner = create_native_owner(db_conn, local_machine=local_machine)
    lease = accept_controller(
        owner, authority=config_authority, database_gate=database_gate, local_machine=local_machine
    )
    db_conn.execute("UPDATE agents_meta SET status='idling' WHERE id=%s", (owner.agent_id,))
    db_conn.commit()
    assert (
        leases.get(
            database, event_bus, lease["id"], attested_caller(lease), local_machine=local_machine
        )["status"]
        == "active"
    )
    db_conn.execute("UPDATE agents_meta SET status='terminated' WHERE id=%s", (owner.agent_id,))
    db_conn.commit()
    assert (
        leases.get(
            database, event_bus, lease["id"], attested_caller(lease), local_machine=local_machine
        )["status"]
        == "expired"
    )
    db_conn.execute("UPDATE agents_meta SET status='idling' WHERE id=%s", (owner.agent_id,))
    db_conn.commit()
    with pytest.raises(leases.ImpersonationError, match="not active"):
        leases.require_active(
            database, lease["id"], attested_caller(lease), local_machine=local_machine
        )


def test_replacement_requires_fresh_consent_before_checkpoint_ack(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    local_machine = machine_identity.name()
    owner = create_native_owner(db_conn, local_machine=local_machine)
    lease = request_controller(
        owner, authority=config_authority, database_gate=database_gate, local_machine=local_machine
    )
    leases.accept(
        database,
        event_bus,
        lease["id"],
        owner.agent_id,
        owner,
        "Handoff brief",
        local_machine=local_machine,
    )
    replacement = RuntimeIncarnation(owner.agent_id, uuid4(), uuid4())
    db_conn.execute(
        "UPDATE agents_meta SET runtime_generation=%s,runtime_owner=%s WHERE id=%s",
        (replacement.generation, replacement.owner, owner.agent_id),
    )
    db_conn.commit()
    state = native_status(replacement, database_gate=database_gate)
    assert state["status"] == "requested"
    assert state["consent_version"] == 2
    leases.accept(
        database,
        event_bus,
        lease["id"],
        owner.agent_id,
        replacement,
        "Handoff brief",
        local_machine=local_machine,
    )
    assert leases.activate(database, event_bus, lease["id"], replacement)["status"] == "active"


def test_expiry_between_driver_read_and_activation_returns_control(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    local_machine = machine_identity.name()
    owner = create_native_owner(db_conn, local_machine=local_machine)
    lease = request_controller(
        owner, authority=config_authority, database_gate=database_gate, local_machine=local_machine
    )
    leases.accept(
        database,
        event_bus,
        lease["id"],
        owner.agent_id,
        owner,
        "Handoff brief",
        local_machine=local_machine,
    )
    assert native_status(owner, database_gate=database_gate)["status"] == "accepted"
    db_conn.execute(
        "UPDATE agent_impersonations SET expires_at=clock_timestamp()-interval '1 second' "
        "WHERE id=%s",
        (lease["id"],),
    )
    db_conn.commit()
    assert leases.activate(database, event_bus, lease["id"], owner)["status"] == "expired"
    assert leases.native_status(database, event_bus, owner.agent_id, owner) is None


def test_renew_replaces_ttl_and_reject_records_reason(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    local_machine = machine_identity.name()
    owner = create_native_owner(db_conn, local_machine=local_machine)
    lease = request_controller(
        owner, authority=config_authority, database_gate=database_gate, local_machine=local_machine
    )
    result = leases.reject(
        database,
        event_bus,
        lease["id"],
        owner.agent_id,
        owner,
        "Finish the critical operation first",
    )
    assert result["rejection_reason"] == "Finish the critical operation first"
    lease = accept_controller(
        owner, authority=config_authority, database_gate=database_gate, local_machine=local_machine
    )
    renewed = leases.renew(
        database,
        event_bus,
        lease["id"],
        attested_caller(lease),
        ttl_seconds=600,
        local_machine=local_machine,
    )
    assert renewed["ttl_seconds"] == 600
    assert renewed["expires_at"] > lease["expires_at"]


def test_reaper_expires_offline_lease_and_keeps_unconsumed_handoff(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    local_machine = machine_identity.name()
    from base.agents.impersonation import maintenance as maintenance
    from base.db import pool

    announced: list[int] = []
    roster_announced: list[int] = []
    patch_announcements(monkeypatch, maintenance, changed=announced, updated=roster_announced)

    owner = create_native_owner(db_conn, local_machine=local_machine)
    lease = accept_controller(
        owner, authority=config_authority, database_gate=database_gate, local_machine=local_machine
    )
    db_conn.execute(
        "UPDATE agent_impersonations SET expires_at=clock_timestamp()-interval '1 second' "
        "WHERE id=%s",
        (lease["id"],),
    )
    db_conn.commit()
    with pool(max_size=2, gate=database_gate) as reaper_pool:
        assert maintenance.reap_impersonations(reaper_pool, database, event_bus) == 1
        assert announced == [owner.agent_id]
        assert roster_announced == [owner.agent_id]
        db_conn.execute(
            "UPDATE agent_impersonations SET ended_at=clock_timestamp()-interval '8 days' WHERE id=%s",
            (lease["id"],),
        )
        db_conn.commit()
        assert maintenance.reap_impersonations(reaper_pool, database, event_bus) == 0
        assert (
            leases.get(
                database,
                event_bus,
                lease["id"],
                attested_caller(lease),
                local_machine=local_machine,
            )["status"]
            == "expired"
        )
        db_conn.execute(
            "UPDATE inbound_messages SET status='done' WHERE agent_id=%s", (owner.agent_id,)
        )
        db_conn.commit()
        maintenance.reap_impersonations(reaper_pool, database, event_bus)
    assert db_conn.execute("SELECT count(*) FROM agent_impersonations").fetchone() == (1,)
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (1,)


def _assert_force_expired_row(
    db_conn: psycopg.Connection,
    owner: RuntimeIncarnation,
    lease: dict[str, Any],
    lease_status: str,
) -> None:
    row = db_conn.execute(
        "SELECT status,ended_at,rejection_reason,summary_inbound_id "
        "FROM agent_impersonations WHERE id=%s",
        (lease["id"],),
    ).fetchone()
    assert row is not None
    status, ended_at, reason, inbound_id = row
    assert (status, reason) == ("expired", "force-expired: ended by an operator")
    assert ended_at is not None
    assert (inbound_id is not None) == (lease_status == "active")
    if inbound_id is not None:
        note = db_conn.execute(
            "SELECT content,kind,source FROM inbound_messages WHERE id=%s", (inbound_id,)
        ).fetchone()
        assert note is not None
        content, kind, source = note
        assert (kind, source) == ("chat", "system:impersonation")
        assert "Control has returned" in content
        assert "handoff" not in content.lower()
        assert "read the" not in content.lower()
        assert db_conn.execute(
            "SELECT status FROM inbound_messages WHERE kind='reminder' AND agent_id=%s",
            (owner.agent_id,),
        ).fetchone() == ("done",)


def _assert_expired_lifecycle_entry(db_conn: psycopg.Connection, lease: dict[str, Any]) -> None:
    entry_row = db_conn.execute(
        "SELECT payload FROM agent_impersonation_entries WHERE lease_id=%s "
        "AND kind='lifecycle' ORDER BY seq DESC LIMIT 1",
        (lease["id"],),
    ).fetchone()
    assert entry_row is not None
    entry = entry_row[0]
    assert entry["status"] == "expired"
    assert entry["source"] == "user_session:administrator"


@pytest.mark.parametrize("lease_status", ["requested", "accepted", "active"])
def test_operator_force_expire_closes_only_observed_session(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    lease_status: str,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    local_machine = machine_identity.name()
    from base.agents.impersonation import maintenance as maintenance
    from base.db import pool

    owner = create_native_owner(db_conn, local_machine=local_machine)
    lease = request_controller(
        owner, authority=config_authority, database_gate=database_gate, local_machine=local_machine
    )
    if lease_status in ("accepted", "active"):
        leases.accept(
            database,
            event_bus,
            lease["id"],
            owner.agent_id,
            owner,
            "Brief",
            local_machine=local_machine,
        )
    if lease_status == "active":
        leases.activate(database, event_bus, lease["id"], owner)
        db_conn.execute(
            "INSERT INTO inbound_messages(agent_id,content,kind,source,payload) "
            "VALUES(%s,'Renew soon','reminder','system',jsonb_build_object('lease_id',%s::text))",
            (owner.agent_id, str(lease["id"])),
        )
        db_conn.commit()
    wakes: list[int] = []
    announcements: list[int] = []
    roster_announcements: list[int] = []

    def record_wake(_db: Database, _bus: EventBus, agent_id: int, _reason: str) -> None:
        wakes.append(agent_id)

    monkeypatch.setattr(maintenance, "publish_inbound_wake", record_wake)
    patch_announcements(
        monkeypatch, maintenance, changed=announcements, updated=roster_announcements
    )
    with pool(max_size=2, gate=database_gate) as gateway_pool:
        assert (
            maintenance.force_expire_impersonation(
                gateway_pool,
                database,
                event_bus,
                owner.agent_id,
                lease["session_id"] + 1,
                "user_session:administrator",
            )
            == "not_open"
        )
        assert (
            maintenance.force_expire_impersonation(
                gateway_pool,
                database,
                event_bus,
                owner.agent_id,
                lease["session_id"],
                "user_session:administrator",
            )
            == "expired"
        )
        assert (
            maintenance.force_expire_impersonation(
                gateway_pool,
                database,
                event_bus,
                owner.agent_id,
                lease["session_id"],
                "user_session:administrator",
            )
            == "not_open"
        )
    assert wakes == [owner.agent_id]
    assert announcements == [owner.agent_id]
    assert roster_announcements == [owner.agent_id]
    _assert_force_expired_row(db_conn, owner, lease, lease_status)
    _assert_expired_lifecycle_entry(db_conn, lease)


def test_operator_force_expire_automatic_session_has_no_manual_end_note(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    local_machine = machine_identity.name()
    from base.agents.impersonation.maintenance import force_expire_impersonation
    from base.db import pool

    owner = create_native_owner(db_conn, local_machine=local_machine)
    lease = leases.request(
        database,
        event_bus,
        owner.agent_id,
        caller=CallerIdentity(kind="external_agent", subject="codex", instance="test"),
        reason="Handle the next message",
        process_metadata=recorded_tree(),
        relay_provider="codex",
        relay_thread_id=str(uuid4()),
        automatic=True,
        name="Automatic takeover",
        executor_name="Codex: test",
        authority=config_authority,
        local_machine=local_machine,
    )
    leases.accept(
        database,
        event_bus,
        lease["id"],
        owner.agent_id,
        owner,
        "Brief",
        local_machine=local_machine,
    )
    leases.activate(database, event_bus, lease["id"], owner)
    with pool(max_size=2, gate=database_gate) as gateway_pool:
        assert (
            force_expire_impersonation(
                gateway_pool,
                database,
                event_bus,
                owner.agent_id,
                lease["session_id"],
                "local_operator",
            )
            == "expired"
        )
    assert db_conn.execute(
        "SELECT summary_inbound_id FROM agent_impersonations WHERE id=%s", (lease["id"],)
    ).fetchone() == (None,)
