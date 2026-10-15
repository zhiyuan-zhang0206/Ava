"""Native controller sessions enforce terminal-state admission and relay specs."""

from typing import Any
from uuid import uuid4

import pytest

from base.agents import impersonation as leases
from base.agents.impersonation.tests.impersonation_support import recorded_tree
from base.api_contracts.caller_identity import CallerIdentity
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.db import Database, create_agent
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.native_process.runtime_incarnation import RuntimeIncarnation


def _agent(db_conn: Any, *, local_machine: str) -> RuntimeIncarnation:
    agent_id = create_agent(db_conn)
    owner = RuntimeIncarnation(agent_id, uuid4(), uuid4())
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine,runtime_generation,runtime_owner,"
        "runtime_kind,lease_expires_at) VALUES(%s,'idling',%s,%s,%s,'process',"
        "clock_timestamp()+interval '10 minutes')",
        (
            agent_id,
            local_machine,
            owner.generation,
            owner.owner,
        ),
    )
    db_conn.commit()
    return owner


def _active(
    owner: RuntimeIncarnation,
    tree: dict[str, Any],
    *,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    local_machine: str,
) -> dict[str, Any]:
    lease = leases.request(
        Database.from_settings(gate=database_gate),
        EventBus.from_settings(),
        owner.agent_id,
        caller=CallerIdentity(kind="external_agent", subject="codex"),
        ttl_seconds=300,
        reason="Attestation coverage",
        process_metadata=tree,
        relay_provider="codex",
        relay_thread_id=str(uuid4()),
        authority=config_authority,
        local_machine=local_machine,
    )
    leases.accept(
        Database.from_settings(gate=database_gate),
        EventBus.from_settings(),
        lease["id"],
        owner.agent_id,
        owner,
        "Handoff brief",
        local_machine=local_machine,
    )
    leases.activate(
        Database.from_settings(gate=database_gate),
        EventBus.from_settings(),
        lease["id"],
        owner,
    )
    return lease


def test_terminal_sessions_report_stale(
    db_conn: Any,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    """A terminal lease is classified as stale-session."""
    local_machine = machine_identity.name()
    owner = _agent(db_conn, local_machine=local_machine)
    lease = _active(
        owner,
        recorded_tree(),
        config_authority=config_authority,
        database_gate=database_gate,
        local_machine=local_machine,
    )
    leases.release(
        database,
        event_bus,
        lease["id"],
        "Done",
        local_machine=local_machine,
    )
    with pytest.raises(leases.ImpersonationError, match="stale-session"):
        leases.require_active(database, lease["id"], local_machine=local_machine)


def test_dsh_request_mints_a_session_relay_credential(
    db_conn: Any,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentity,
) -> None:
    """dsh runs its relay in the controller session, like claude: the request
    mints the scoped credential, and a thread id or codex remote is refused."""
    local_machine = machine_identity.name()
    owner = _agent(db_conn, local_machine=local_machine)
    caller = CallerIdentity(kind="external_agent", subject="dsh")
    with pytest.raises(ValueError, match="dsh relay routes to its owner"):
        leases.request(
            database,
            event_bus,
            owner.agent_id,
            caller=caller,
            reason="dsh",
            relay_provider="dsh",
            relay_thread_id="thread",
            authority=config_authority,
            local_machine=local_machine,
        )
    lease = leases.request(
        database,
        event_bus,
        owner.agent_id,
        caller=caller,
        ttl_seconds=300,
        reason="dsh",
        process_metadata=recorded_tree(),
        relay_provider="dsh",
        authority=config_authority,
        local_machine=local_machine,
    )
    assert lease["relay_provider"] == "dsh"
    assert lease["relay_token"]


def test_antigravity_request_records_host_relay_spec(
    db_conn: Any,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentity,
) -> None:
    """antigravity records its conversation thread id and provisions its relay at activation."""
    local_machine = machine_identity.name()
    owner = _agent(db_conn, local_machine=local_machine)
    caller = CallerIdentity(kind="external_agent", subject="antigravity")
    thread_id = str(uuid4())
    with pytest.raises(ValueError, match="thread id"):
        leases.request(
            database,
            event_bus,
            owner.agent_id,
            caller=caller,
            reason="antigravity",
            relay_provider="antigravity",
            authority=config_authority,
            local_machine=local_machine,
        )
    lease = leases.request(
        database,
        event_bus,
        owner.agent_id,
        caller=caller,
        ttl_seconds=300,
        reason="antigravity",
        process_metadata=recorded_tree(),
        relay_provider="antigravity",
        relay_thread_id=thread_id,
        authority=config_authority,
        local_machine=local_machine,
    )
    assert lease["relay_provider"] == "antigravity"
    assert lease["relay_thread_id"] == thread_id
    assert "relay_token" not in lease
