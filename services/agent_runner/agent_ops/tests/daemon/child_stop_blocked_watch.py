from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path

from base.daemon.health import start_health_server
from base.events.live.bus import EventBus, EventBusConfig
from base.native_process.loaded_commit import LoadedCommit
from services.agent_runner.agent_ops.tests.child_graceful_stop import run_ops_runner_entry
from services.agent_runner.agent_ops.to_user_watch import watch_to_user


def _blocked_worker(blocked: Path) -> None:
    blocked.write_text("blocked")
    threading.Event().wait()


async def _held_cleanup(draining: Path, seconds: float) -> None:
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
    held_s = float(argv[6])

    async def run() -> None:
        workspaces = home / "workspaces"
        (workspaces / "7").mkdir(parents=True)
        (workspaces / "7" / "to_user.md").write_text("x")
        bus = EventBus(EventBusConfig(redis_url="redis://127.0.0.1:1/0", events_channel="test"))
        server = await start_health_server(
            "ops", port, image=LoadedCommit(source_root=home, sha=None)
        )
        try:
            async with server, asyncio.TaskGroup() as group:
                group.create_task(watch_to_user(workspaces, bus))
                group.create_task(asyncio.to_thread(_blocked_worker, blocked))
                group.create_task(_held_cleanup(draining, held_s))
                ready.write_text("ready")
                await asyncio.Event().wait()
        finally:
            clean.write_text("clean")

    run_ops_runner_entry(run)


if __name__ == "__main__":
    main(sys.argv[1:])
