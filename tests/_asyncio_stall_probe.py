from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from typing import Any

import pytest
from _pytest.faulthandler import fault_handler_stderr_fd_key

DUMP_MARGIN_SECONDS = 2.0

_MIN_DELAY_SECONDS = 0.05


def faulthandler_timeout(config: pytest.Config) -> float:
    try:
        return float(config.getini("faulthandler_timeout") or 0.0)
    except ValueError:
        return 0.0


def awaiter_chain_lines(task: asyncio.Task[Any]) -> list[str]:
    lines: list[str] = []
    awaitable: object = task.get_coro()
    while awaitable is not None:
        frame: Any = getattr(awaitable, "cr_frame", None) or getattr(awaitable, "ag_frame", None)
        if frame is not None:
            code = frame.f_code
            lines.append(f'  File "{code.co_filename}", line {frame.f_lineno}, in {code.co_name}')
        awaitable = getattr(awaitable, "cr_await", None) or getattr(awaitable, "ag_await", None)
    return lines


def _dump_pending_chains(stderr_fd: int, nodeid: str, timeout: float) -> None:
    tasks = sorted(asyncio.all_tasks(), key=lambda task: task.get_name())
    lines = [
        f"=== asyncio stall probe: {nodeid} ({len(tasks)} pending asyncio task(s), "
        f"dumping {DUMP_MARGIN_SECONDS:g}s before the {timeout:g}s faulthandler dump) ==="
    ]
    for task in tasks:
        lines.append(f"--- task {task.get_name()} ---")
        lines.extend(awaiter_chain_lines(task) or ["  <no frames captured>"])
    data = ("\n".join(lines) + "\n").encode("utf-8", errors="backslashreplace")
    while data:
        data = data[os.write(stderr_fd, data) :]


@contextmanager
def probe_window(nodeid: str, timeout: float, stderr_fd: int) -> Generator[None]:
    if timeout <= 0.0:
        yield
        return
    start = time.monotonic()
    handles: list[asyncio.TimerHandle] = []
    original_events = asyncio.events.new_event_loop
    original_top = asyncio.new_event_loop

    def probing_factory(
        original: Callable[[], asyncio.AbstractEventLoop],
    ) -> Callable[[], asyncio.AbstractEventLoop]:
        def factory() -> asyncio.AbstractEventLoop:
            loop = original()
            delay = max(
                timeout - DUMP_MARGIN_SECONDS - (time.monotonic() - start), _MIN_DELAY_SECONDS
            )
            handles.append(loop.call_later(delay, _dump_pending_chains, stderr_fd, nodeid, timeout))
            return loop

        return factory

    asyncio.events.new_event_loop = probing_factory(original_events)
    asyncio.new_event_loop = probing_factory(original_top)
    try:
        yield
    finally:
        asyncio.events.new_event_loop = original_events
        asyncio.new_event_loop = original_top
        for handle in handles:
            handle.cancel()


@pytest.hookimpl(wrapper=True, trylast=True)
def pytest_runtest_protocol(item: pytest.Item) -> Generator[None, object, object]:
    timeout = faulthandler_timeout(item.config)
    if timeout <= 0.0:
        return (yield)
    stderr_fd = item.config.stash[fault_handler_stderr_fd_key]
    with probe_window(item.nodeid, timeout, stderr_fd):
        return (yield)
