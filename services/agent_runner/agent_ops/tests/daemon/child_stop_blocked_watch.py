"""Child entry for the stop-under-a-blocked-watcher test (task #5129).

Usage: `child_stop_blocked_watch.py <home> <port> <ready> <blocked> <draining>
<clean> <block-s> <held-s>`

Runs the ops daemon's real stop path (via `child_graceful_stop.run_ops_runner_entry`)
with the user-context watcher stuck in its own setup for <block-s> — the shape
that owned the loop for minutes on 2026-10-14 — plus one unrelated
default-executor thread that never finishes. The daemon must answer `/healthz`
while that setup blocks, and a stop's SIGTERM, retried while the drain is held
open for <held-s>, must still end the process inside its stop window.

Markers: <ready> once the health server answers, <blocked> inside the blocked
setup, <draining> once the drain is inside its held leg, <clean> once cleanup
reached the serving body.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

from watchfiles import Change

from base.daemon.health import start_health_server
from base.events.live.bus import EventBus, EventBusConfig
from base.native_process.loaded_commit import LoadedCommit
from services.agent_runner.agent_ops.tests.child_graceful_stop import run_ops_runner_entry
from services.agent_runner.agent_ops.to_user_watch import watch_to_user


def _blocked_watch(
    blocked: Path, block_s: float
) -> Callable[..., Iterator[set[tuple[Change, str]]]]:
    """A watch source whose setup cost is a sleep on the thread that pays it."""

    def watch_source(*paths: Path, **_kwargs: object) -> Iterator[set[tuple[Change, str]]]:
        blocked.write_text("blocked")
        time.sleep(block_s)
        yield {(Change.modified, str(paths[0] / "7" / "to_user.html"))}
        threading.Event().wait()  # then a live watch round that never ends

    return watch_source


async def _held_cleanup(draining: Path, seconds: float) -> None:
    """Hold the drain open so a retried stop lands inside it.

    Synchronous on purpose: the drain cancels every task it finds, which cuts an
    awaited leg short, so a blocking hold is the only one a test can time.
    """
    try:
        await asyncio.Event().wait()
    finally:
        draining.write_text("draining")
        time.sleep(seconds)


def main(argv: list[str]) -> None:
    home, port, ready, blocked, draining, clean = (
        Path(argv[0]),
        int(argv[1]),
        Path(argv[2]),
        Path(argv[3]),
        Path(argv[4]),
        Path(argv[5]),
    )
    block_s, held_s = float(argv[6]), float(argv[7])

    async def run() -> None:
        workspaces = home / "workspaces"
        (workspaces / "7").mkdir(parents=True)
        (workspaces / "7" / "to_user.html").write_text("x")
        bus = EventBus(EventBusConfig(redis_url="redis://127.0.0.1:1/0", events_channel="test"))
        server = await start_health_server(
            "ops", port, image=LoadedCommit(source_root=home, sha=None)
        )
        try:
            async with server, asyncio.TaskGroup() as group:
                group.create_task(
                    watch_to_user(workspaces, bus, watch_source=_blocked_watch(blocked, block_s))
                )
                group.create_task(asyncio.to_thread(threading.Event().wait))  # never finishes
                group.create_task(_held_cleanup(draining, held_s))
                ready.write_text("ready")
                await asyncio.Event().wait()
        finally:
            clean.write_text("clean")

    run_ops_runner_entry(run)


if __name__ == "__main__":
    main(sys.argv[1:])
