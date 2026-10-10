"""Native controller sessions enforce generation and terminal-state admission."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import psutil
import pytest

from base.agents import impersonation as leases
from base.agents.messages.caller_identity import CallerIdentity
from base.cluster.machine import machine_name
from base.config.service_read import ConfigAuthority
from base.db import Database, create_agent
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.native_process import ownership
from base.native_process.runtime_incarnation import RuntimeIncarnation
from tests.impersonation_support import (
    attested_caller,
    native_identity,
    recorded_tree,
    unrelated_caller,
)


def _agent(db_conn: Any) -> RuntimeIncarnation:
    agent_id = create_agent(db_conn)
    owner = RuntimeIncarnation(agent_id, uuid4(), uuid4())
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine,runtime_generation,runtime_owner,"
        "runtime_kind,lease_expires_at) VALUES(%s,'idling',%s,%s,%s,'process',"
        "clock_timestamp()+interval '10 minutes')",
        (
            agent_id,
            machine_name(),
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
    )
    leases.accept(
        Database.from_settings(gate=database_gate),
        EventBus.from_settings(),
        lease["id"],
        owner.agent_id,
        owner,
        "Handoff brief",
    )
    leases.activate(
        Database.from_settings(gate=database_gate),
        EventBus.from_settings(),
        lease["id"],
        owner,
    )
    return lease


def test_generation_crossing_is_refused(
    db_conn: Any,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    *,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
) -> None:
    """A caller attested for one generation cannot drive another's session id."""
    first = _active(
        _agent(db_conn),
        recorded_tree(),
        config_authority=config_authority,
        database_gate=database_gate,
    )
    second_tree = recorded_tree()
    second_tree["ancestors"] = [
        {
            "pid": 4341,
            "name": "zsh",
            "executable": "/bin/zsh",
            **native_identity(1999.0),
            "parent_pid": 4340,
        },
        {
            "pid": 4340,
            "name": "codex",
            "executable": "/opt/codex",
            **native_identity(1998.0),
            "parent_pid": 1,
        },
    ]
    second = _active(
        _agent(db_conn), second_tree, config_authority=config_authority, database_gate=database_gate
    )

    def process(pid: int) -> SimpleNamespace:
        return SimpleNamespace(pid=pid, status=lambda: psutil.STATUS_RUNNING)

    monkeypatch.setattr(psutil, "Process", process)
    monkeypatch.setattr(ownership, "stable_create_time", Mock(return_value=1998.0))
    monkeypatch.setattr(
        ownership, "pid_starttime_ticks", Mock(return_value=native_identity(1998.0)["starttime"])
    )
    with pytest.raises(leases.ImpersonationError, match="chain-mismatch"):
        leases.require_active(database, second["id"], attested_caller(first))


def test_terminal_sessions_report_stale_without_attestation(
    db_conn: Any,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
) -> None:
    """A terminal lease is classified before any anchor work: native/TTL recovery
    never depends on the dead controller tree (the second, incarnation-held gate)."""
    owner = _agent(db_conn)
    lease = _active(
        owner, recorded_tree(), config_authority=config_authority, database_gate=database_gate
    )
    leases.release(database, event_bus, lease["id"], attested_caller(lease), "Done")
    with pytest.raises(leases.ImpersonationError, match="stale-session"):
        leases.require_active(database, lease["id"], unrelated_caller())


def test_dsh_request_mints_a_session_relay_credential(
    db_conn: Any, database: Database, event_bus: EventBus, *, config_authority: ConfigAuthority
) -> None:
    """dsh runs its relay in the controller session, like claude: the request
    mints the scoped credential, and a thread id or codex remote is refused."""
    owner = _agent(db_conn)
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
    )
    assert lease["relay_provider"] == "dsh"
    assert lease["relay_token"]
