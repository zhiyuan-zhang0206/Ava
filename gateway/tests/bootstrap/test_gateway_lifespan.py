"""Gateway lifespan joins its own flushers before closing their database pools."""

import asyncio
import importlib
from collections.abc import Callable
from contextlib import AsyncExitStack
from typing import Any

import psycopg
import pytest
from fastapi import FastAPI


@pytest.fixture
def flusher_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> asyncio.Queue[asyncio.Task[Any]]:
    gateway_app = importlib.import_module("gateway.app")
    started: asyncio.Queue[asyncio.Task[Any]] = asyncio.Queue()

    async def flush(*_args: object) -> None:
        task = asyncio.current_task()
        assert task is not None
        started.put_nowait(task)
        try:
            await asyncio.Event().wait()
        finally:
            # Joining must include asynchronous cleanup, not just cancel().
            await asyncio.sleep(0)

    monkeypatch.setattr(gateway_app.latency, "latency_flusher", flush)
    monkeypatch.setattr(gateway_app.rejection_log, "auth401_flusher", flush)

    def register_cron(*, enabled_reader: Callable[[], bool]) -> None:
        return None

    monkeypatch.setattr(gateway_app, "register_os_cron", register_cron)
    return started


async def test_overlapping_lifespans_join_their_original_flushers(
    db_conn: psycopg.Connection[Any], flusher_tasks: asyncio.Queue[asyncio.Task[Any]]
) -> None:
    del db_conn  # Provision the native, isolated database for the real lifespan.
    gateway_app = importlib.import_module("gateway.app")
    app = FastAPI()
    async with asyncio.timeout(5), AsyncExitStack() as cleanup:
        async with gateway_app.lifespan(app):
            original = [await flusher_tasks.get(), await flusher_tasks.get()]
            # Other app.state resources retain their current nested-lifespan
            # behavior; release the original handles after the lifespan joins.
            cleanup.callback(app.state.runtime_metrics.stop)
            cleanup.push_async_callback(app.state.grafana_client.aclose)
            cleanup.push_async_callback(app.state.insights_client.aclose)
            cleanup.callback(app.state.db_pool.close)
            cleanup.callback(app.state.control_db_pool.close)
            async with gateway_app.lifespan(app):
                replacement = [await flusher_tasks.get(), await flusher_tasks.get()]
            assert all(task.done() for task in replacement)
            assert all(not task.done() for task in original)
        assert all(task.done() for task in original)


async def test_client_close_failure_still_joins_flushers_before_pool_close(
    db_conn: psycopg.Connection[Any],
    flusher_tasks: asyncio.Queue[asyncio.Task[Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del db_conn
    gateway_app = importlib.import_module("gateway.app")
    app = FastAPI()
    failure = RuntimeError("proxy client close failed")
    close_checks: list[bool] = []
    tasks: list[asyncio.Task[Any]] = []
    pools: list[Any] = []

    async with asyncio.timeout(5):
        with pytest.raises(ExceptionGroup) as raised:
            async with gateway_app.lifespan(app):
                tasks.extend([await flusher_tasks.get(), await flusher_tasks.get()])
                pools.extend([app.state.db_pool, app.state.control_db_pool])
                original_close: Callable[[], Any] = app.state.insights_client.aclose

                async def failed_close() -> None:
                    await original_close()
                    raise failure

                monkeypatch.setattr(app.state.insights_client, "aclose", failed_close)
                for pool in pools:
                    original_pool_close = pool.close

                    def checked_close(close: Callable[[], None] = original_pool_close) -> None:
                        close_checks.append(all(task.done() for task in tasks))
                        close()

                    monkeypatch.setattr(pool, "close", checked_close)

    assert raised.value.exceptions == (failure,)
    assert close_checks == [True, True]
    assert all(task.done() for task in tasks)
    assert all(pool.closed for pool in pools)
