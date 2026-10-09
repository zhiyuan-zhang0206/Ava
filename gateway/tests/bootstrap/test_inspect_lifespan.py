"""Gateway cancellation drains a real inspector SQL call before pool closure."""

import asyncio
import importlib
import threading
from collections.abc import Callable
from typing import Any

import psycopg
import pytest
from fastapi import FastAPI


async def test_lifespan_waits_for_cancelled_request_sql_before_pool_close(
    db_conn: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    del db_conn
    gateway_app = importlib.import_module("gateway.app")
    monkeypatch.setattr(gateway_app, "register_os_cron", lambda: None)
    app = FastAPI()
    ready = asyncio.Event()
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    pool_closed_after_sql: list[bool] = []

    async def service() -> None:
        async with gateway_app.lifespan(app):
            for pool in (app.state.db_pool, app.state.control_db_pool):
                original = pool.close

                def checked_close(close: Callable[[], None] = original) -> None:
                    pool_closed_after_sql.append(finished.is_set())
                    close()

                monkeypatch.setattr(pool, "close", checked_close)
            ready.set()
            await asyncio.Event().wait()

    def query() -> int:
        with app.state.db_pool.connection(timeout=1) as conn:
            assert conn.execute("SELECT 1").fetchone()[0] == 1
            started.set()
            assert release.wait(timeout=5)
            result = conn.execute("SELECT 2").fetchone()[0]
        finished.set()
        return result

    running = asyncio.create_task(service())
    request = None
    try:
        async with asyncio.timeout(10):
            await ready.wait()
            request = asyncio.create_task(
                app.state.inspect_query_cache.get_or_load_async(
                    "native-sql", query, ttl_s=0, now=lambda: 0
                )
            )
            assert await asyncio.to_thread(started.wait, 1)
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            running.cancel()
            assert not (await asyncio.wait({running}, timeout=0.02))[0]
            assert pool_closed_after_sql == []
            assert not finished.is_set()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await running
            assert finished.is_set()
            assert pool_closed_after_sql == [True, True]
    finally:
        release.set()
        running.cancel()
        await asyncio.gather(*[task for task in (running, request) if task], return_exceptions=True)


async def test_native_sql_error_does_not_cancel_gateway_service(
    db_conn: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    del db_conn
    gateway_app = importlib.import_module("gateway.app")
    monkeypatch.setattr(gateway_app, "register_os_cron", lambda: None)
    app = FastAPI()
    ready, stop = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    reports: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: reports.append(context))

    async def service() -> None:
        async with gateway_app.lifespan(app):
            ready.set()
            await stop.wait()

    def query() -> None:
        with app.state.db_pool.connection(timeout=1) as conn:
            conn.execute("SELECT * FROM inspection_lifetime_missing_table")

    running = asyncio.create_task(service())
    try:
        async with asyncio.timeout(10):
            await ready.wait()
            with pytest.raises(psycopg.errors.UndefinedTable) as raised:
                await app.state.inspect_query_cache.get_or_load_async(
                    "bad-sql", query, ttl_s=0, now=lambda: 0
                )
            assert not running.done()
            with app.state.db_pool.connection(timeout=1) as conn:
                assert conn.execute("SELECT 1").fetchone()[0] == 1
            assert reports[0]["exception"] is raised.value
            stop.set()
            await running
    finally:
        stop.set()
        await asyncio.gather(running, return_exceptions=True)
        loop.set_exception_handler(previous)
