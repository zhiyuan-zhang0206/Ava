from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from psycopg_pool import AsyncConnectionPool

from agent import db
from agent.graph.llm_errors import FatalProviderError
from agent.turn.runloop import _handle_fatal_llm_error
from base.agents.context import MeivaContext
from base.agents.recovery import breaker
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
from services.agent_runner.agent_host.tests.host_policy import configured_policy


def _permanent_rejection() -> FatalProviderError:
    return FatalProviderError(
        "provider permanently rejected (HTTP 400): content risk",
        error_class="permanent",
        provider="deepseek",
        status=400,
    )


def _context(pool: AsyncConnectionPool, *, database_gate: ProcessDbGate) -> MeivaContext:
    return MeivaContext(
        ops_pool=pool,
        llm=MagicMock(),
        event_publisher=MagicMock(),
        agent=AgentSlices.resolve(default_reader=configured_policy().default_reader),
        db=Database.from_settings(gate=database_gate),
        bus=EventBus.from_settings(),
        catalog=build_model_catalog(),
        clock_factory=configured_policy().clock_factory,
    )


async def _reject(
    pool: AsyncConnectionPool, agent_id: int, *, database_gate: ProcessDbGate
) -> None:
    await _handle_fatal_llm_error(
        _permanent_rejection(),
        _context(pool, database_gate=database_gate),
        agent_id=agent_id,
        occurred_at=datetime(2026, 9, 16, 6, 0, tzinfo=UTC),
    )


def _sole_record(loguru_records: list[dict[str, Any]], fragment: str) -> dict[str, Any]:
    matches = [r for r in loguru_records if fragment in r["message"]]
    assert len(matches) == 1
    return matches[0]


async def test_streak_write_failure_keeps_its_traceback(
    redis_url: str,
    aops_pool: AsyncConnectionPool,
    loguru_records: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    async def _boom(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("recovery breaker store down")

    monkeypatch.setattr(breaker, "record_permanent_reject_turn", _boom)

    await _reject(
        aops_pool,
        spawn_agent(
            spawner="user",
            catalog=model_catalog,
            authority=config_authority,
            database_gate=database_gate,
            machine=machine_identity,
        ),
        database_gate=database_gate,
    )

    record = _sole_record(loguru_records, "failed to record a permanent-rejection streak")
    assert record["exception"] is not None
    assert record["exception"].type is RuntimeError


async def test_halt_suppression_failure_keeps_its_traceback(
    redis_url: str,
    aops_pool: AsyncConnectionPool,
    loguru_records: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    async def _streak(*_args: object, **_kwargs: object) -> int:
        return 99

    async def _boom(*_args: object, **_kwargs: object) -> bool:
        raise RuntimeError("suppression write down")

    async def _noop(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(breaker, "record_permanent_reject_turn", _streak)
    monkeypatch.setattr(breaker, "halt_automatic_recovery", _boom)
    monkeypatch.setattr(db, "enqueue_fatal_provider_report_to_nearest_alive_ancestor", _noop)

    await _reject(
        aops_pool,
        spawn_agent(
            spawner="user",
            catalog=model_catalog,
            authority=config_authority,
            database_gate=database_gate,
            machine=machine_identity,
        ),
        database_gate=database_gate,
    )

    record = _sole_record(loguru_records, "failed to suppress automatic wakes")
    assert record["exception"] is not None
    assert record["exception"].type is RuntimeError


async def test_ancestor_report_failure_keeps_its_traceback(
    redis_url: str,
    aops_pool: AsyncConnectionPool,
    loguru_records: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    async def _streak(*_args: object, **_kwargs: object) -> int:
        return 99

    async def _halt(*_args: object, **_kwargs: object) -> bool:
        return True

    async def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("enqueue down")

    monkeypatch.setattr(breaker, "record_permanent_reject_turn", _streak)
    monkeypatch.setattr(breaker, "halt_automatic_recovery", _halt)
    monkeypatch.setattr(db, "enqueue_fatal_provider_report_to_nearest_alive_ancestor", _boom)

    await _reject(
        aops_pool,
        spawn_agent(
            spawner="user",
            catalog=model_catalog,
            authority=config_authority,
            database_gate=database_gate,
            machine=machine_identity,
        ),
        database_gate=database_gate,
    )

    record = _sole_record(loguru_records, "failed to enqueue the recovery-halt report")
    assert record["exception"] is not None
    assert record["exception"].type is RuntimeError
