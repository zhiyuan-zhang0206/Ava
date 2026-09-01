"""The hosted daemon's periodic page-liveness scan — `_page_reconcile_forever`.

Task #2260: hosted agents have no per-agent `page_reconcile_loop` (the
daemon drives turns directly and never runs `agent/loop.py:main()`), so the
daemon runs the heartbeat-independent scan itself — one pass over every
agent with open pages, every heartbeat interval, skipping agents another
path (their heartbeat scan) already reconciled.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from services.agent_host.daemon import _page_reconcile_forever


async def _run_loop_briefly(task: asyncio.Task[object], seconds: float) -> None:
    """Run the daemon-loop task for `seconds`, then cancel it (the loop never
    exits on its own) and swallow its CancelledError."""
    await asyncio.sleep(seconds)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_page_reconcile_forever_runs_periodically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The daemon scans on the heartbeat-interval cadence regardless of agent
    activity — a busy hosted agent's pages still heal."""
    from shared.config import settings

    calls: list[tuple[object, float]] = []

    async def _fake_all(pool, *, interval_s, event_publisher=None):
        calls.append((pool, interval_s))  # pyright: ignore[reportUnknownArgumentType]

    monkeypatch.setattr("agent.startup.reconcile_all_open_pages", _fake_all)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(settings.daemon, "heartbeat_interval_seconds", 0.01)

    task = asyncio.create_task(_page_reconcile_forever(object()))  # type: ignore[arg-type]
    await _run_loop_briefly(task, 0.06)

    assert len(calls) >= 2
    _pool, interval_s = calls[0]
    assert interval_s == 0.01


async def test_page_reconcile_forever_survives_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raising pass must not kill the daemon loop — it logs and waits for the
    next interval, mirroring the process-mode loop's self-protection."""
    from shared.config import settings

    calls = 0

    async def _boom(pool, *, interval_s, event_publisher=None):
        nonlocal calls
        calls += 1
        raise RuntimeError("boom")

    monkeypatch.setattr("agent.startup.reconcile_all_open_pages", _boom)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(settings.daemon, "heartbeat_interval_seconds", 0.01)

    task = asyncio.create_task(_page_reconcile_forever(object()))  # type: ignore[arg-type]
    # Cancels cleanly -> the task survived; had a pass killed it, the await
    # would re-raise the exception instead.
    await _run_loop_briefly(task, 0.05)
    assert calls >= 2


async def test_page_reconcile_forever_runs_immediately_on_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first pass runs at daemon start, not after a full interval — the
    hosted equivalent of the process-mode boot scan, so a daemon restart
    (platform update) starts healing dead pages at once (#1312 QA nit)."""
    from shared.config import settings

    calls = 0

    async def _fake_all(pool, *, interval_s, event_publisher=None):
        nonlocal calls
        calls += 1

    monkeypatch.setattr("agent.startup.reconcile_all_open_pages", _fake_all)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(settings.daemon, "heartbeat_interval_seconds", 60.0)

    task = asyncio.create_task(_page_reconcile_forever(object()))  # type: ignore[arg-type]
    # With a 60s interval, a pass within 0.2s proves the immediate first scan
    # (a sleep-first loop would have zero calls here).
    await _run_loop_briefly(task, 0.2)
    assert calls == 1


async def test_spawn_background_tasks_includes_page_reconciler() -> None:
    """The daemon's background-task wiring must include the page reconciler —
    a regression dropping it would silently reopen the busy-hosted-agent
    dead-page gap (#1312 QA P2: 'delete create_task' mutation was green)."""
    import services.agent_host.daemon as daemon_mod

    tasks = daemon_mod._spawn_background_tasks(object())  # type: ignore[arg-type]
    try:
        assert set(tasks) == {"plugins_watch", "page_reconciler"}
        assert isinstance(tasks["page_reconciler"], asyncio.Task)
    finally:
        for task in tasks.values():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            for task in tasks.values():
                await task
