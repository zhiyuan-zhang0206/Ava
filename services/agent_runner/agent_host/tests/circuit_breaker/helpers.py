"""Circuit-breaker test helpers: the recording publisher, overflow state and rejection drivers shared by the breaker tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import MagicMock

import psycopg
from langchain_core.messages import HumanMessage, SystemMessage
from psycopg_pool import AsyncConnectionPool

from agent.graph.llm_errors import FatalProviderError
from agent.state import AgentState, CircuitState
from agent.turn.runloop import _handle_fatal_llm_error
from base.agents.context import AvaContext
from base.cluster.machine import MachineIdentity
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.events.live.publisher import AgentEventPublisher
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.lm.plugin_providers import build_model_catalog
from services.agent_runner.agent_host.tests.host_policy import configured_policy
from tests.fixtures.unit.identity import machine_identity as machine_identity
from tests.fixtures.units import spawn_agent


class RecordingPublisher:
    """Minimal typed event sink for fatal-error live-event assertions."""

    def __init__(self) -> None:
        self.payloads: list[str] = []

    def emit(self, payload: str) -> None:
        self.payloads.append(payload)


def overflow_state(breaker_reason: str | None = None) -> AgentState:
    """An agent state sitting past the provider's context ceiling, with the
    circuit breaker optionally open (the default closed)."""
    circuit = CircuitState()
    if breaker_reason is not None:
        circuit = CircuitState(
            open=True, reason=breaker_reason, opened_at="2026-08-29T00:00:00+00:00"
        )
    return AgentState(
        messages=[
            SystemMessage(content="<sys>"),
            *(HumanMessage(content=f"history-{i}") for i in range(10)),
        ],
        halted=True,
        circuit=circuit,
    )


def breaker_ctx(*, database_gate: ProcessDbGate) -> AvaContext:
    """An AvaContext for `_handle_fatal_llm_error` — no ops_pool, so the
    best-effort event-log write is skipped (unit tests have no DB)."""
    return AvaContext(
        ops_pool=None,
        llm=MagicMock(),
        event_publisher=MagicMock(),
        agent=AgentSlices.resolve(default_reader=configured_policy().default_reader),
        db=Database.from_settings(gate=database_gate),
        bus=EventBus.from_settings(),
        catalog=build_model_catalog(),
        clock_factory=configured_policy().clock_factory,
    )


def permanent_rejection() -> FatalProviderError:
    """A permanent 400 in the 6260 shape; the body is deliberately distinctive
    so the tests can prove no rejected text reaches the ancestor report."""
    return FatalProviderError(
        "Content Exists Risk: do not replay this rejected history",
        error_class="permanent",
        provider="deepseek",
        status=400,
    )


async def reject_turn(
    aops_pool: AsyncConnectionPool,
    agent_id: int,
    *,
    publisher: RecordingPublisher,
    exc: FatalProviderError | None = None,
    database_gate: ProcessDbGate,
) -> None:
    await _handle_fatal_llm_error(
        exc if exc is not None else permanent_rejection(),
        AvaContext(
            ops_pool=aops_pool,
            llm=MagicMock(),
            event_publisher=cast(AgentEventPublisher, publisher),
            agent=AgentSlices.resolve(default_reader=configured_policy().default_reader),
            db=Database.from_settings(gate=database_gate),
            bus=EventBus.from_settings(),
            catalog=build_model_catalog(),
            clock_factory=configured_policy().clock_factory,
        ),
        agent_id=agent_id,
        occurred_at=datetime(2026, 9, 16, 6, 0, tzinfo=UTC),
    )


def spawn_child_under_idling_ancestor(
    db_conn: psycopg.Connection,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    local_machine: MachineIdentity,
) -> tuple[int, int]:
    """A live idling ancestor and its child; returns (ancestor_id, child_id)."""
    ancestor_id = spawn_agent(
        spawner="user",
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=local_machine,
    )
    child_id = spawn_agent(
        spawner=f"agent:{ancestor_id}",
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=local_machine,
    )
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET status = 'idling', "
            "lease_expires_at = now() + interval '10 minutes' WHERE id = %s",
            (ancestor_id,),
        )
    db_conn.commit()
    return ancestor_id, child_id


def ancestor_halt_notes(db_conn: psycopg.Connection, ancestor_id: int) -> list[str]:
    reports = [
        r[0]
        for r in db_conn.execute(
            "SELECT content FROM inbound_messages WHERE agent_id = %s AND kind = 'system_note' "
            "ORDER BY id",
            (ancestor_id,),
        ).fetchall()
    ]
    return [note for note in reports if "blocked after a permanent provider rejection" in note]


def recovery_halt_errors(publisher: RecordingPublisher) -> list[dict[str, Any]]:
    errors = [json.loads(payload) for payload in publisher.payloads]
    return [
        err
        for err in errors
        if err.get("blocked") and "Automatic recovery is halted" in err.get("content", "")
    ]
