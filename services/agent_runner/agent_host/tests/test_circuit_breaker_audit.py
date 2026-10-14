"""The circuit-breaker audit event is recorded in `audit_events`, and a failed write
is reported without undoing the breaker (the state is already set)."""

from __future__ import annotations

from unittest.mock import MagicMock

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent.graph.llm_errors import FatalProviderError
from agent.state import CircuitState
from agent.turn.runloop import _handle_fatal_llm_error
from base.agents.context import AvaContext
from base.agents.tests.units import spawn_agent
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.packages.plugin_providers import build_model_catalog
from base.telemetry import audit_events
from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from services.agent_runner.agent_host.tests.host_policy import configured_policy


def _overflow() -> FatalProviderError:
    return FatalProviderError(
        "provider permanently rejected (HTTP 400): context length exceeded",
        error_class="permanent",
        provider="anthropic",
        status=400,
        context_overflow=True,
    )


async def test_the_breaker_open_event_is_recorded_in_audit_events(
    redis_url: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    agent_id = spawn_agent(
        spawner="user",
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )

    await _handle_fatal_llm_error(
        _overflow(),
        AvaContext(
            ops_pool=aops_pool,
            llm=MagicMock(),
            event_publisher=MagicMock(),
            agent=AgentSlices.resolve(default_reader=configured_policy().default_reader),
            db=Database.from_settings(gate=database_gate),
            bus=EventBus.from_settings(),
            catalog=build_model_catalog(),
            clock_factory=configured_policy().clock_factory,
        ),
        agent_id=agent_id,
    )

    rows = db_conn.execute(
        "SELECT source, attributes->>'action', attributes->>'reason' FROM audit_events "
        "WHERE agent_id = %s AND event_name = 'circuit_breaker'",
        (agent_id,),
    ).fetchall()
    assert set(rows) == {
        ("system", "open", "context_overflow"),
        ("system", "halt", "context_overflow"),
    }


async def test_a_failed_audit_write_is_reported_and_does_not_undo_the_open_breaker(
    redis_url: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    emitted_events: EmittedEvents,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    agent_id = spawn_agent(
        spawner="user",
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )

    async def refuse(_pool: object, _event: object) -> None:
        raise RuntimeError("audit write failed")

    monkeypatch.setattr(audit_events, "record_audit_standalone_async", refuse)

    emitted_events.clear()  # spawning the agent above emitted its own events

    update = await _handle_fatal_llm_error(
        _overflow(),
        AvaContext(
            ops_pool=aops_pool,
            llm=MagicMock(),
            event_publisher=MagicMock(),
            agent=AgentSlices.resolve(default_reader=configured_policy().default_reader),
            db=Database.from_settings(gate=database_gate),
            bus=EventBus.from_settings(),
            catalog=build_model_catalog(),
            clock_factory=configured_policy().clock_factory,
        ),
        agent_id=agent_id,
    )

    circuit = update["circuit"]
    assert isinstance(circuit, CircuitState)
    assert circuit.open is True
    reported = [a["event_name"] for a in emitted_events.attributes("audit_write_failed")]
    assert reported == ["circuit_breaker", "circuit_breaker"]
    count = db_conn.execute(
        "SELECT count(*) FROM audit_events WHERE agent_id = %s AND event_name = 'circuit_breaker'",
        (agent_id,),
    ).fetchone()
    assert count == (0,)
