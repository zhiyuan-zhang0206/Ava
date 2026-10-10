"""The circuit-breaker audit event is recorded in `audit_events`, and a failed write
is reported without undoing the breaker (the state is already set)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent.graph.llm_errors import FatalProviderError
from agent.state import CircuitState
from agent.turn.runloop import _handle_fatal_llm_error
from base import telemetry
from base.agents.context import AvaContext
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.lm.plugin_providers import build_model_catalog
from services.agent_runner.agent_host.tests.host_policy import configured_policy
from tests.fixtures.units import spawn_agent


def _overflow() -> FatalProviderError:
    return FatalProviderError(
        "provider permanently rejected (HTTP 400): context length exceeded",
        error_class="permanent",
        provider="anthropic",
        status=400,
        context_overflow=True,
    )


async def test_the_breaker_open_event_is_recorded_in_audit_events(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
) -> None:
    agent_id = spawn_agent(spawner="user", catalog=model_catalog, authority=config_authority)

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
    assert rows == [("system", "open", "context_overflow")]


async def test_a_failed_audit_write_is_reported_and_does_not_undo_the_open_breaker(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
) -> None:
    agent_id = spawn_agent(spawner="user", catalog=model_catalog, authority=config_authority)
    reported: list[str] = []

    async def refuse(_pool: object, _event: object) -> None:
        raise RuntimeError("audit write failed")

    monkeypatch.setattr("base.telemetry.audit_events.record_audit_standalone_async", refuse)

    def emit(_category: str, name: str, **kwargs: Any) -> None:
        if name == "audit_write_failed":
            reported.append(kwargs["attributes"]["event_name"])

    monkeypatch.setattr(telemetry, "emit", emit)

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
    assert reported == ["circuit_breaker"]
    count = db_conn.execute(
        "SELECT count(*) FROM audit_events WHERE agent_id = %s AND event_name = 'circuit_breaker'",
        (agent_id,),
    ).fetchone()
    assert count == (0,)
