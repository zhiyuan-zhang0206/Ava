"""The idempotent pass retries only its bounded connection failures."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from services.agent_runner.agent_ops import daemon
from services.agent_runner.agent_ops.tests.test_daemon import _noop_sleep


@pytest.mark.asyncio
async def test_dispatch_idempotent_retries_operational_error_then_succeeds(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pass that dies on a closed connection is re-run; the outcome is returned."""
    calls: list[int] = []

    async def _flaky_pass(
        kind,
        payload,
        key,
        pool,
        *,
        active_ops: daemon.ActiveOps,
        workers: daemon.maintenance_activity.WorkerFutures,
        executor: ThreadPoolExecutor,
    ):  # type: ignore[no-untyped-def]
        calls.append(1)
        if len(calls) < 3:
            raise psycopg.OperationalError("the connection is closed")
        return "completed", {"ok": True}

    monkeypatch.setattr(daemon, "_dispatch_idempotent_pass", _flaky_pass)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(daemon, "_sleep", _noop_sleep)
    status, result = await daemon._dispatch_idempotent(
        "spawn-launch",
        {"agent_id": 1},
        "key-1",
        ConnectionPool(open=False),
        active_ops={},
        workers=set(),
        executor=op_executor,
    )
    assert status == "completed"
    assert result == {"ok": True}
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_dispatch_idempotent_gives_up_after_attempts(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A persistently dying connection raises after the bounded attempts."""
    calls: list[int] = []

    async def _always_dead(
        kind,
        payload,
        key,
        pool,
        *,
        active_ops: daemon.ActiveOps,
        workers: daemon.maintenance_activity.WorkerFutures,
        executor: ThreadPoolExecutor,
    ):  # type: ignore[no-untyped-def]
        calls.append(1)
        raise psycopg.OperationalError("the connection is closed")

    monkeypatch.setattr(daemon, "_dispatch_idempotent_pass", _always_dead)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(daemon, "_sleep", _noop_sleep)
    with pytest.raises(psycopg.OperationalError):
        await daemon._dispatch_idempotent(
            "spawn-launch",
            {"agent_id": 1},
            "key-2",
            ConnectionPool(open=False),
            active_ops={},
            workers=set(),
            executor=op_executor,
        )
    assert len(calls) == daemon._DISPATCH_RETRY_ATTEMPTS


@pytest.mark.asyncio
async def test_dispatch_idempotent_propagates_non_operational_error(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any non-OperationalError propagates on the first pass — no retry."""
    calls: list[int] = []

    async def _boom(
        kind,
        payload,
        key,
        pool,
        *,
        active_ops: daemon.ActiveOps,
        workers: daemon.maintenance_activity.WorkerFutures,
        executor: ThreadPoolExecutor,
    ):  # type: ignore[no-untyped-def]
        calls.append(1)
        raise ValueError("not a connection problem")

    monkeypatch.setattr(daemon, "_dispatch_idempotent_pass", _boom)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(daemon, "_sleep", _noop_sleep)
    with pytest.raises(ValueError):
        await daemon._dispatch_idempotent(
            "spawn-launch",
            {"agent_id": 1},
            "key-3",
            ConnectionPool(open=False),
            active_ops={},
            workers=set(),
            executor=op_executor,
        )
    assert len(calls) == 1
