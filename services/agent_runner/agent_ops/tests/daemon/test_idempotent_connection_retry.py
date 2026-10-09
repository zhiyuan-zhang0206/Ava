"""The idempotent pass retries only its bounded connection failures."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.config.service_read import ConfigAuthority
from base.lm.catalog import ModelCatalog
from services.agent_runner.agent_ops import daemon
from services.agent_runner.agent_ops.tests.test_daemon import _noop_sleep


@pytest.mark.asyncio
async def test_dispatch_idempotent_retries_operational_error_then_succeeds(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    """A pass that dies on a closed connection is re-run; the outcome is returned."""
    calls: list[int] = []

    async def _flaky_pass(
        kind: str,
        payload: dict[str, Any],
        key: str,
        pool: ConnectionPool | None,
        *,
        active_ops: daemon.ActiveOps,
        workers: daemon.maintenance_activity.WorkerFutures,
        executor: ThreadPoolExecutor,
        catalog: ModelCatalog,
        authority: ConfigAuthority,
    ) -> tuple[str, dict[str, object]]:
        calls.append(1)
        if len(calls) < 3:
            raise psycopg.OperationalError("the connection is closed")
        return "completed", {"ok": True}

    monkeypatch.setattr(daemon, "_dispatch_idempotent_pass", _flaky_pass)
    monkeypatch.setattr(daemon, "_sleep", _noop_sleep)
    status, result = await daemon._dispatch_idempotent(
        "spawn-launch-v2",
        {"launch_attempt_id": "00000000-0000-0000-0000-000000000001", "agent_id": 1},
        "key-1",
        ConnectionPool(open=False),
        active_ops={},
        workers=set(),
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
    )
    assert status == "completed"
    assert result == {"ok": True}
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_dispatch_idempotent_gives_up_after_attempts(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    """A persistently dying connection raises after the bounded attempts."""
    calls: list[int] = []

    async def _always_dead(
        kind: str,
        payload: dict[str, Any],
        key: str,
        pool: ConnectionPool | None,
        *,
        active_ops: daemon.ActiveOps,
        workers: daemon.maintenance_activity.WorkerFutures,
        executor: ThreadPoolExecutor,
        catalog: ModelCatalog,
        authority: ConfigAuthority,
    ) -> tuple[str, dict[str, object]]:
        calls.append(1)
        raise psycopg.OperationalError("the connection is closed")

    monkeypatch.setattr(daemon, "_dispatch_idempotent_pass", _always_dead)
    monkeypatch.setattr(daemon, "_sleep", _noop_sleep)
    with pytest.raises(psycopg.OperationalError):
        await daemon._dispatch_idempotent(
            "spawn-launch-v2",
            {"launch_attempt_id": "00000000-0000-0000-0000-000000000001", "agent_id": 1},
            "key-2",
            ConnectionPool(open=False),
            active_ops={},
            workers=set(),
            executor=op_executor,
            catalog=model_catalog,
            authority=config_authority,
        )
    assert len(calls) == daemon._DISPATCH_RETRY_ATTEMPTS


@pytest.mark.asyncio
async def test_dispatch_idempotent_propagates_non_operational_error(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    """Any non-OperationalError propagates on the first pass — no retry."""
    calls: list[int] = []

    async def _boom(
        kind: str,
        payload: dict[str, Any],
        key: str,
        pool: ConnectionPool | None,
        *,
        active_ops: daemon.ActiveOps,
        workers: daemon.maintenance_activity.WorkerFutures,
        executor: ThreadPoolExecutor,
        catalog: ModelCatalog,
        authority: ConfigAuthority,
    ) -> tuple[str, dict[str, object]]:
        calls.append(1)
        raise ValueError("not a connection problem")

    monkeypatch.setattr(daemon, "_dispatch_idempotent_pass", _boom)
    monkeypatch.setattr(daemon, "_sleep", _noop_sleep)
    with pytest.raises(ValueError):
        await daemon._dispatch_idempotent(
            "spawn-launch-v2",
            {"launch_attempt_id": "00000000-0000-0000-0000-000000000001", "agent_id": 1},
            "key-3",
            ConnectionPool(open=False),
            active_ops={},
            workers=set(),
            executor=op_executor,
            catalog=model_catalog,
            authority=config_authority,
        )
    assert len(calls) == 1
