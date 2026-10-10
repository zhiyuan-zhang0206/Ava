"""`_record_permanent_reject_outcome`'s failure branches keep their tracebacks.

Three best-effort steps follow a permanent provider rejection (agent/turn/
runloop.py): recording the streak, suppressing automatic wakes at the halt
threshold, and enqueueing the recovery-halt report to an ancestor. Each
failure branch logged with loguru's `exc_info=True`, which loguru has no
parameter for — the kwarg rode the record's `extra` and the traceback was
lost. These lock the `.opt(exception=True)` shape on all three branches
(task #4979).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from psycopg_pool import AsyncConnectionPool

from agent.graph.llm_errors import FatalProviderError
from agent.turn.runloop import _handle_fatal_llm_error
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


def _permanent_rejection() -> FatalProviderError:
    return FatalProviderError(
        "provider permanently rejected (HTTP 400): content risk",
        error_class="permanent",
        provider="deepseek",
        status=400,
    )


def _context(pool: AsyncConnectionPool, *, database_gate: ProcessDbGate) -> AvaContext:
    return AvaContext(
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
    aops_pool: AsyncConnectionPool,
    loguru_records: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
) -> None:
    """Branch 1: the streak write failed — the warning keeps the cause (task #4979)."""

    async def _boom(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("recovery breaker store down")

    monkeypatch.setattr("base.agents.recovery.breaker.record_permanent_reject_turn", _boom)

    await _reject(
        aops_pool,
        spawn_agent(
            spawner="user",
            catalog=model_catalog,
            authority=config_authority,
            database_gate=database_gate,
        ),
        database_gate=database_gate,
    )

    record = _sole_record(loguru_records, "failed to record a permanent-rejection streak")
    assert record["exception"] is not None
    assert record["exception"].type is RuntimeError


async def test_halt_suppression_failure_keeps_its_traceback(
    aops_pool: AsyncConnectionPool,
    loguru_records: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
) -> None:
    """Branch 2: suppressing automatic wakes failed (task #4979)."""

    async def _streak(*_args: object, **_kwargs: object) -> int:
        return 99

    async def _boom(*_args: object, **_kwargs: object) -> bool:
        raise RuntimeError("suppression write down")

    async def _noop(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr("base.agents.recovery.breaker.record_permanent_reject_turn", _streak)
    monkeypatch.setattr("base.agents.recovery.breaker.halt_automatic_recovery", _boom)
    monkeypatch.setattr("agent.db.enqueue_fatal_provider_report_to_nearest_alive_ancestor", _noop)

    await _reject(
        aops_pool,
        spawn_agent(
            spawner="user",
            catalog=model_catalog,
            authority=config_authority,
            database_gate=database_gate,
        ),
        database_gate=database_gate,
    )

    record = _sole_record(loguru_records, "failed to suppress automatic wakes")
    assert record["exception"] is not None
    assert record["exception"].type is RuntimeError


async def test_ancestor_report_failure_keeps_its_traceback(
    aops_pool: AsyncConnectionPool,
    loguru_records: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
) -> None:
    """Branch 3: enqueueing the recovery-halt report failed (task #4979)."""

    async def _streak(*_args: object, **_kwargs: object) -> int:
        return 99

    async def _halt(*_args: object, **_kwargs: object) -> bool:
        return True

    async def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("enqueue down")

    monkeypatch.setattr("base.agents.recovery.breaker.record_permanent_reject_turn", _streak)
    monkeypatch.setattr("base.agents.recovery.breaker.halt_automatic_recovery", _halt)
    monkeypatch.setattr("agent.db.enqueue_fatal_provider_report_to_nearest_alive_ancestor", _boom)

    await _reject(
        aops_pool,
        spawn_agent(
            spawner="user",
            catalog=model_catalog,
            authority=config_authority,
            database_gate=database_gate,
        ),
        database_gate=database_gate,
    )

    record = _sole_record(loguru_records, "failed to enqueue the recovery-halt report")
    assert record["exception"] is not None
    assert record["exception"].type is RuntimeError
