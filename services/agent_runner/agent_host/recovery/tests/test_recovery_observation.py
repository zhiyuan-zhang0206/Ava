"""Recovery deadlines, prolonged warnings and complete attempt accounting."""

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import psycopg
import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from agent.graph.tests.cursor_fixture import _fresh_snapshot_cursor as _fresh_snapshot_cursor
from base.agents.observation.db_wait import DatabaseWaits
from base.config import settings
from base.config.service_read import ConfigAuthority
from base.lm.catalog import ModelCatalog
from base.native_process.runtime_incarnation import RuntimeIncarnation
from services.agent_runner.agent_host import db_recovery
from services.agent_runner.agent_host.recovery.tests.test_hosted_db_recovery import (
    _admit,
    _graph,
)
from services.agent_runner.agent_host.recovery.tests.test_hosted_db_recovery import (
    isolate as isolate,
)


@pytest.fixture
def recovery_observation(monkeypatch: pytest.MonkeyPatch) -> tuple[list[float], Mock, AsyncMock]:
    clock = [1000.0]
    # Replace only this module's clock; real pool and asyncio deadlines still run.
    monkeypatch.setattr(db_recovery, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    log = Mock()
    monkeypatch.setattr(db_recovery, "logger", log)

    async def advance_backoff(_delay: float) -> None:
        clock[0] += 10.0

    backoff = AsyncMock(side_effect=advance_backoff)
    monkeypatch.setattr(db_recovery.RecoveryInterrupt, "wait_backoff", backoff)
    return clock, log, backoff


@pytest.mark.parametrize(
    ("error", "stage_seconds", "expected_error_type"),
    [
        (PoolTimeout("unavailable"), 290.0, "PoolTimeout"),
        (psycopg.errors.AdminShutdown(), 300.0, "AdminShutdown"),
        (TimeoutError("unavailable"), 290.0, "PoolTimeout"),
    ],
)
async def test_recovery_budget_abandons_at_attempt_boundary(
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    recovery_observation: tuple[list[float], Mock, AsyncMock],
    error: Exception,
    stage_seconds: float,
    expected_error_type: str,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    clock, log, backoff = recovery_observation
    monkeypatch.setattr(settings.daemon, "host_db_recovery_budget_seconds", 600.0)
    incarnation = await _admit(
        aops_pool, model_catalog=model_catalog, config_authority=config_authority
    )
    graph, saver = await _graph(aops_pool, incarnation.agent_id, AsyncMock())
    attempts = 0
    waits = DatabaseWaits()

    async def failed_repair(_graph: Any, _agent: int) -> None:
        nonlocal attempts
        attempts += 1
        assert attempts <= 2, "a spent ladder must not start another repair attempt"
        clock[0] += stage_seconds
        raise error

    monkeypatch.setattr(db_recovery, "repair_dangling_tool_use_at_startup", failed_repair)
    with pytest.raises(db_recovery.DatabaseRecoveryBudgetExceededError, match="after 2 attempts"):
        await db_recovery.recover_database(
            pool=aops_pool,
            graph=graph,
            checkpointer=saver,
            incarnation=incarnation,
            database_waits=waits,
            peek_lock=asyncio.Lock(),
            work=None,
        )
    assert attempts == backoff.await_count == 2
    assert waits.snapshot(incarnation.agent_id) is None
    log.error.assert_called_once_with(
        "host checkpoint recovery abandoned",
        agent_id=incarnation.agent_id,
        attempts=2,
        total_elapsed_seconds=2 * (stage_seconds + 10.0),
        final_phase="tool_state_repair",
        last_error_type=expected_error_type,
        last_sqlstate=error.sqlstate if isinstance(error, psycopg.Error) else None,
    )
    assert (
        sum(c.args[0] == "host checkpoint recovery retry" for c in log.warning.call_args_list) == 2
    )
    assert all(c.args[0] != "host turn checkpoint recovered" for c in log.info.call_args_list)
    assert any(
        c.args[0]
        == (
            "host checkpoint recovery stage failed"
            if isinstance(error, psycopg.OperationalError) and not isinstance(error, PoolTimeout)
            else "host checkpoint recovery stage timed out"
        )
        and c.kwargs["phase"] == "tool_state_repair"
        and c.kwargs["duration_ms"] >= 0
        for c in log.warning.call_args_list
    )


@pytest.mark.parametrize(("attempt_limit", "seconds_limit"), [(2, 300.0), (99, 50.0), (2, 50.0)])
async def test_recovery_prolonged_warns_once_at_first_threshold_crossing(
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    recovery_observation: tuple[list[float], Mock, AsyncMock],
    attempt_limit: int,
    seconds_limit: float,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    clock, log, backoff = recovery_observation
    monkeypatch.setattr(settings.daemon, "host_db_recovery_prolonged_attempts", attempt_limit)
    monkeypatch.setattr(settings.daemon, "host_db_recovery_prolonged_seconds", seconds_limit)
    incarnation = await _admit(
        aops_pool, model_catalog=model_catalog, config_authority=config_authority
    )
    graph, saver = await _graph(aops_pool, incarnation.agent_id, AsyncMock())
    flush = db_recovery.flush_checkpoint
    attempts = 0

    async def flaky_flush(checkpointer: AsyncPostgresSaver, agent: int) -> None:
        nonlocal attempts
        attempts += 1
        clock[0] += 20.0
        if attempts <= 3:
            raise PoolTimeout("checkpoint unavailable")
        await flush(checkpointer, agent)

    monkeypatch.setattr(db_recovery, "flush_checkpoint", flaky_flush)
    await db_recovery.recover_database(
        pool=aops_pool,
        graph=graph,
        checkpointer=saver,
        incarnation=incarnation,
        database_waits=DatabaseWaits(),
        peek_lock=asyncio.Lock(),
        work=None,
    )
    warnings = [
        c for c in log.warning.call_args_list if c.args[0] == "host checkpoint recovery prolonged"
    ]
    assert len(warnings) == 1
    assert warnings[0].kwargs == {
        "agent_id": incarnation.agent_id,
        "attempts": 2,
        "total_elapsed_seconds": 50.0,
        "phase": "checkpoint_flush",
        "error_type": "PoolTimeout",
        "sqlstate": None,
    }
    assert attempts == 4
    assert backoff.await_count == 3
    log.error.assert_not_called()
    assert sum(c.args[0] == "host turn checkpoint recovered" for c in log.info.call_args_list) == 1


@pytest.mark.parametrize("failures", [0, 2])
async def test_recovery_summary_counts_all_attempts_and_backoff_time(
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    recovery_observation: tuple[list[float], Mock, AsyncMock],
    failures: int,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    clock, log, backoff = recovery_observation
    incarnation = await _admit(
        aops_pool, model_catalog=model_catalog, config_authority=config_authority
    )
    graph, saver = await _graph(aops_pool, incarnation.agent_id, AsyncMock())
    refresh = db_recovery._refresh_owner
    failed_probes = 0
    waits = DatabaseWaits()

    async def flaky_probe(pool: AsyncConnectionPool, original: RuntimeIncarnation) -> None:
        nonlocal failed_probes
        clock[0] += 1.0
        if failed_probes < failures:
            failed_probes += 1
            raise PoolTimeout("owner unavailable")
        await refresh(pool, original)

    monkeypatch.setattr(db_recovery, "_refresh_owner", flaky_probe)
    await db_recovery.recover_database(
        pool=aops_pool,
        graph=graph,
        checkpointer=saver,
        incarnation=incarnation,
        database_waits=waits,
        peek_lock=asyncio.Lock(),
        work=None,
    )
    assert backoff.await_count == failures
    assert waits.snapshot(incarnation.agent_id) is not None
    recovered = [
        c for c in log.info.call_args_list if c.args[0] == "host turn checkpoint recovered"
    ]
    assert len(recovered) == 1
    assert recovered[0].kwargs == {
        "agent_id": incarnation.agent_id,
        "attempt": failures + 1,
        "elapsed_seconds": 3.0,
        "total_attempts": failures + 1,
        "total_elapsed_seconds": failures * 11.0 + 3.0,
    }
    stages = [
        c for c in log.info.call_args_list if c.args[0] == "host checkpoint recovery stage complete"
    ]
    assert len(stages) == 6
    assert {c.kwargs["phase"] for c in stages} == {
        "owner_probe",
        "checkpoint_flush",
        "inbound_reconciliation",
        "owner_revalidation",
        "tool_state_repair",
        "repaired_owner_validation",
    }
    assert all(c.kwargs["outcome"] == "success" and c.kwargs["duration_ms"] >= 0 for c in stages)
    assert all(
        c.args[0] != "host checkpoint recovery prolonged" for c in log.warning.call_args_list
    )
    log.error.assert_not_called()
