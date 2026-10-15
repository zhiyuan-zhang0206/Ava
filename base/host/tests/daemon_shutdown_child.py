import asyncio
import os
import signal
import time
from pathlib import Path
from typing import Any, Protocol

MARKERS_ENV = "DAEMON_SHUTDOWN_TEST_MARKERS"
INTERNAL_STOP_ENV = "DAEMON_SHUTDOWN_TEST_INTERNAL_STOP"
CLEANUP_FAILURE_ENV = "DAEMON_SHUTDOWN_TEST_CLEANUP_FAILURE"

WEDGE_SECONDS = 600.0


class DaemonModule(Protocol):
    run: Any

    def main(self) -> None: ...


def _mark(name: str) -> None:
    with Path(os.environ[MARKERS_ENV]).open("a", encoding="utf-8") as markers:
        markers.write(f"{name}\n")


async def _internal_stop() -> None:
    await asyncio.sleep(0.1)
    signal.raise_signal(signal.SIGTERM)


async def _wait_for_stop() -> None:
    if os.environ.get(INTERNAL_STOP_ENV) == "1":
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(_internal_stop())
            await asyncio.Event().wait()
    else:
        while True:
            await asyncio.sleep(0.1)


def run_wedged_daemon(mod: DaemonModule) -> None:

    async def wedge_run(**_process_inputs: object) -> None:
        try:
            asyncio.get_running_loop().run_in_executor(None, time.sleep, WEDGE_SECONDS)
            _mark("wedge-scheduled")
            _mark("ready")
            await _wait_for_stop()
        finally:
            _mark("cleanup-started")
            await asyncio.sleep(0.05)
            _mark("cleanup-ran")
            if os.environ.get(CLEANUP_FAILURE_ENV) == "1":
                raise RuntimeError("surrogate cleanup failure")

    mod.run = wedge_run
    mod.main()
