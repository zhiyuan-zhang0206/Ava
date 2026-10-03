"""Takeover barriers: consent, resource closure, checkpoint ordering and replay."""

from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from langchain_core.messages import HumanMessage
from psycopg_pool import AsyncConnectionPool

from base.agents.context import AvaContext
from base.db import Database
from base.events.live.bus import EventBus
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.native_process.turn_identity import bind_turn_identity
from tests.impersonation_support import recorded_tree


@pytest.fixture
def gate_ctx(database: Database, event_bus: EventBus) -> AvaContext:
    return AvaContext(db=database, bus=event_bus)


@pytest.fixture
def incarnation() -> Iterator[RuntimeIncarnation]:
    token = RuntimeIncarnation(42, uuid4(), uuid4())
    with bind_turn_identity(token.agent_id, incarnation=token):
        yield token


def _session(status: str = "active", **values: Any) -> dict[str, Any]:
    return {
        "id": "lease-1",
        "automatic": False,
        "handoff_applied_at": None,
        "process_metadata": {},
        "source": "external_agent:codex:task1",
        "status": status,
        "reason": "Finish the assigned task",
        "consent_version": 1,
        "plugin_delta": [],
        "delta_version": 0,
        "applied_version": 0,
        "relay_provider": "codex",
        "relay_thread_id": "thread-1",
        "relay_codex_remote": None,
        "relay_heartbeat_at": datetime.now(UTC),
        "relay_last_failure_at": None,
        **values,
    }


# ── Relay establishment gate and supervision ────────────────────────────────


def _relay_session(
    status: str = "accepted", *, provider: str = "codex", **values: Any
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "relay_provider": provider,
        "relay_thread_id": "thread-1" if provider == "codex" else None,
        "relay_codex_remote": None,
        "relay_heartbeat_at": None,
        "relay_last_failure_at": None,
    }
    base.update(values)
    return _session(status, **base)


async def test_successor_admission_resets_a_stale_accepted_binding(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    database: Database,
    event_bus: EventBus,
) -> None:
    """Issue #2052: an accepted (not yet active) lease whose accepting
    incarnation died restarts at 'requested' under the successor admission —
    the same crash-before-ACK semantics as the lazy native_status path."""
    from agent.ownership.hosted import admit_hosted_runtime
    from base.agents import impersonation as leases
    from base.agents.messages.caller_identity import CallerIdentity
    from base.cluster.machine import machine_name
    from tests.fixtures.units import spawn_agent

    agent_id = spawn_agent()
    first = await admit_hosted_runtime(
        aops_pool, agent_id, machine_name(), uuid4(), expected_from="idling", db=database
    )
    assert first is not None
    lease = leases.request(
        database,
        event_bus,
        agent_id,
        caller=CallerIdentity(kind="external_agent", subject="codex", instance="test"),
        ttl_seconds=3600,
        reason="Handle the next message",
        process_metadata=recorded_tree(),
        relay_provider="codex",
        relay_thread_id=str(uuid4()),
    )
    leases.accept(database, event_bus, lease["id"], agent_id, first, "Handoff brief")
    db_conn.execute(
        "UPDATE agents_meta SET lease_expires_at = clock_timestamp() - interval '1 second' "
        "WHERE id=%s",
        (agent_id,),
    )
    db_conn.commit()
    successor = await admit_hosted_runtime(
        aops_pool, agent_id, machine_name(), uuid4(), expected_from="running", db=database
    )
    assert successor is not None
    assert db_conn.execute(
        "SELECT status,accepted_generation,accepted_owner,consent_version "
        "FROM agent_impersonations WHERE id=%s",
        (lease["id"],),
    ).fetchone() == ("requested", None, None, 2)


def test_resume_note_pending_tracks_the_trailing_end_note() -> None:
    """The claim's fresh-window waiver keys on the newest message being the
    delivered end-of-session note; any newer message ends the pending resume."""
    from agent.impersonation_handoff import resume_note_pending

    note = HumanMessage(content="session ended", id="impersonation-handoff:7:0")
    state = SimpleNamespace(impersonation_handoff_id="7:0", messages=[note])
    assert resume_note_pending(state)
    state.messages.append(HumanMessage(content="next task", id="m-next"))
    assert not resume_note_pending(state)
    state.impersonation_handoff_id = None
    assert not resume_note_pending(state)
    assert not resume_note_pending(SimpleNamespace(impersonation_handoff_id="9:0", messages=[]))
