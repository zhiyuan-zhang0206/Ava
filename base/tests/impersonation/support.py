"""Explicit native-owner and controller-lease factories for database contract tests.

The caller supplies its machine label and admission gate. These factories retain
the production consent, incarnation and controller-attestation transitions.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import psycopg

from base.agents import impersonation as leases
from base.agents.impersonation.tests.impersonation_support import recorded_tree
from base.api_contracts.caller_identity import CallerIdentity
from base.config.service_read import ConfigAuthority
from base.db import Database, create_agent
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.native_process.runtime_incarnation import RuntimeIncarnation

__all__ = ["accept_controller", "create_native_owner", "native_status", "request_controller"]


def create_native_owner(conn: psycopg.Connection, *, local_machine: str) -> RuntimeIncarnation:
    """Persist this caller's native incarnation on its explicitly supplied machine."""
    agent_id = create_agent(conn)
    owner = RuntimeIncarnation(agent_id, uuid4(), uuid4())
    conn.execute(
        "INSERT INTO agents_meta(id,status,machine,runtime_generation,runtime_owner,"
        "runtime_kind,lease_expires_at) VALUES(%s,'idling',%s,%s,%s,'process',"
        "clock_timestamp()+interval '10 minutes')",
        (agent_id, local_machine, owner.generation, owner.owner),
    )
    conn.commit()
    return owner


def native_status(owner: RuntimeIncarnation, *, database_gate: ProcessDbGate) -> dict[str, Any]:
    """Read the incarnation-gated native state without authenticating a controller."""
    result = leases.native_status(
        Database.from_settings(gate=database_gate), EventBus.from_settings(), owner.agent_id, owner
    )
    assert result is not None
    return result


def request_controller(
    owner: RuntimeIncarnation,
    *,
    authority: ConfigAuthority,
    provider: str = "codex",
    thread: str | None = None,
    database_gate: ProcessDbGate,
    local_machine: str,
) -> dict[str, Any]:
    """Request the recorded controller tree's consent on the caller's machine."""
    kwargs: dict[str, Any] = {}
    if provider in ("codex", "antigravity"):
        kwargs["relay_thread_id"] = thread or str(uuid4())
    return leases.request(
        Database.from_settings(gate=database_gate),
        EventBus.from_settings(),
        owner.agent_id,
        caller=CallerIdentity(kind="external_agent", subject="codex", instance="test"),
        ttl_seconds=300,
        reason="Handle the next message",
        process_metadata=recorded_tree(),
        relay_provider=provider,
        authority=authority,
        **kwargs,
        local_machine=local_machine,
    )


def accept_controller(
    owner: RuntimeIncarnation,
    *,
    authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    local_machine: str,
) -> dict[str, Any]:
    """Accept and activate consent against the same incarnation and machine."""
    lease = request_controller(
        owner, authority=authority, database_gate=database_gate, local_machine=local_machine
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
        Database.from_settings(gate=database_gate), EventBus.from_settings(), lease["id"], owner
    )
    return lease
