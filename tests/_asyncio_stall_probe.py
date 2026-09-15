"""Stall forensics for the CI backend shards: dump every asyncio task's await
chain shortly before pytest's faulthandler deadline fires (task #3513).

The shards run with ``-o faulthandler_timeout=300``, so a test that outlives
300s makes pytest's faulthandler plugin dump every thread's traceback. In the
2026-09-13/14 stalls (runs 34905554147, 34928778559) those dumps showed an
xdist worker's main thread parked in the event loop's ``select()`` with no test
frame anywhere in the worker: the thread dump cannot name the stuck test, so
attributing the stall meant reconstructing the shard's schedule by hand.

This probe closes exactly that gap. While a test protocol runs under a
faulthandler deadline, every event loop created for it (``asyncio.run`` and the
pytest-asyncio runner fixture both build loops through
``asyncio.new_event_loop``) gets a timer due ``DUMP_MARGIN_SECONDS`` before that
deadline. The timer callback prints the full await chain of every pending task
— the ``awaiter_chain_lines`` pattern from ``agent/graph/_node_log.py``, copied
so the test side imports no agent code — a few seconds before the thread dump
lands, so one grep of the CI log names the stuck test and the leaf it awaits.

The dump targets the fd `_pytest.faulthandler` stashed at configure time, not
``sys.__stderr__``: pytest's fd capture redirects fd 2 into a per-test temp
file while the test runs, and a test killed inside the deadline never reaches
the point where pytest replays that file — writing to fd 2 would bury the dump
in exactly the case it exists for. The stash dup is made before capture starts,
and faulthandler's own dump writes to the same one.

No new dependency, no change to the deadline, and inert without one: with
``faulthandler_timeout`` unset (the local default) nothing is patched and
nothing is armed. Loaded as a plugin: ``tests/conftest.py`` names it in
``pytest_plugins``.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from typing import Any

import pytest
from _pytest.faulthandler import fault_handler_stderr_fd_key

# Seconds before the faulthandler deadline the probe dumps. Both dumps target
# the same fd while faulthandler writes from its watchdog thread, so the margin
# keeps one single write from interleaving with the other dump.
DUMP_MARGIN_SECONDS = 2.0

# Floor for the timer delay: a test whose setup already consumed the whole
# deadline still gets its dump as soon as its loop exists.
_MIN_DELAY_SECONDS = 0.05


def faulthandler_timeout(config: pytest.Config) -> float:
    """The per-test faulthandler deadline in seconds, 0.0 when it is unset.

    Same read as ``_pytest.faulthandler.get_timeout_config_value``. The ini
    option belongs to the faulthandler plugin, so a run that disabled it
    (``-p no:faulthandler``) reads as "no deadline" instead of failing every
    test's protocol — the whole probe is relative to that deadline.
    """
    try:
        return float(config.getini("faulthandler_timeout") or 0.0)
    except ValueError:
        return 0.0


def awaiter_chain_lines(task: asyncio.Task[Any]) -> list[str]:
    """Render a task's full await chain, outermost coroutine first.

    Pattern-copied from ``agent/graph/_node_log.py::awaiter_chain_lines``: for
    a suspended task ``Task.get_stack()`` returns only the outermost suspension
    frame; walking ``cr_await`` / ``ag_await`` descends through every nested
    coroutine / async generator down to the innermost awaited line — the fact
    the probe exists for. Kept as a copy so the test side never imports agent
    code into the session.
    """
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
    """Loop-timer callback: print every pending task's await chain to `stderr_fd`.

    Runs ON the loop — a loop that is idle in ``select()`` still wakes for
    timers, which is the stall shape this probe exists for — so
    ``asyncio.all_tasks()`` answers for the loop that stalled. One single write
    (done in fd-level chunks, since a short write must not split the block):
    the dump has to reach the log as a whole and must not interleave with
    faulthandler's own write from its watchdog thread.
    """
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
    """Arm the stall probe for one test protocol; no-op when ``timeout <= 0``.

    While the window is open, ``asyncio.new_event_loop`` and
    ``asyncio.events.new_event_loop`` (normally one object; both names are
    rebound so either lookup hits the wrapper) call through to the original and
    arm the probe timer on the loop they return. The delay is measured from the
    window's start — the same instant ``_pytest.faulthandler`` arms its own
    per-test dump — so slow fixture setup cannot push the chain dump past the
    thread dump. Leaving the window restores both names and cancels every timer
    armed inside it: the exact lifetime faulthandler gives its own dump.
    """
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
    """Arm the stall probe for this test's protocol window (task #3513).

    Mirrors `_pytest.faulthandler`'s own protocol wrapper, which arms the
    per-test thread dump the probe is timed against: the window opens before
    fixture setup — where the test's event loop is created — and closes after
    teardown. `-o faulthandler_timeout=N` (the CI shards) is the only switch;
    unset/0 makes it a no-op. The stash entry exists whenever a deadline does:
    both come from the faulthandler plugin's own configure pass.
    """
    timeout = faulthandler_timeout(item.config)
    if timeout <= 0.0:
        return (yield)
    stderr_fd = item.config.stash[fault_handler_stderr_fd_key]
    with probe_window(item.nodeid, timeout, stderr_fd):
        return (yield)
