"""Shared agent/status/request/active helpers for the base/tests/test_impersonation files; split from base/tests/test_impersonation.py (task #4922)."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import psycopg

from base.agents import impersonation as leases
from base.agents.messages.caller_identity import CallerIdentity
from base.cluster.machine import machine_name
from base.db import Database, create_agent
from base.events.live.bus import EventBus
from base.native_process.runtime_incarnation import RuntimeIncarnation
from tests.impersonation_support import recorded_tree


def _agent(conn: psycopg.Connection) -> RuntimeIncarnation:
    agent_id = create_agent(conn)
    owner = RuntimeIncarnation(agent_id, uuid4(), uuid4())
    conn.execute(
        "INSERT INTO agents_meta(id,status,machine,runtime_generation,runtime_owner,"
        "runtime_kind,lease_expires_at) VALUES(%s,'idling',%s,%s,%s,'process',"
        "clock_timestamp()+interval '10 minutes')",
        (agent_id, machine_name(), owner.generation, owner.owner),
    )
    conn.commit()
    return owner


def _status(owner: RuntimeIncarnation) -> dict[str, Any]:
    result = leases.native_status(
        Database.from_settings(), EventBus.from_settings(), owner.agent_id, owner
    )
    assert result is not None
    return result


def _request(
    owner: RuntimeIncarnation, *, provider: str = "codex", thread: str | None = None
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if provider == "codex":
        kwargs["relay_thread_id"] = thread or str(uuid4())
    return leases.request(
        Database.from_settings(),
        EventBus.from_settings(),
        owner.agent_id,
        caller=CallerIdentity(kind="external_agent", subject="codex", instance="test"),
        ttl_seconds=300,
        reason="Handle the next message",
        process_metadata=recorded_tree(),
        relay_provider=provider,
        **kwargs,
    )


def _active(owner: RuntimeIncarnation) -> dict[str, Any]:
    lease = _request(owner)
    leases.accept(
        Database.from_settings(),
        EventBus.from_settings(),
        lease["id"],
        owner.agent_id,
        owner,
        "Handoff brief",
    )
    leases.activate(Database.from_settings(), EventBus.from_settings(), lease["id"], owner)
    return lease
