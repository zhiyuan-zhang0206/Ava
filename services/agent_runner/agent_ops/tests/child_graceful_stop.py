import asyncio
import contextlib
import sys
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from base.daemon.shutdown import install_graceful_shutdown


def run_ops_runner_entry(body: Callable[[], Awaitable[None]]) -> None:
    from services.agent_runner.agent_ops import daemon

    async def ops_run(**_kwargs: Any) -> None:
        await body()

    daemon.init_gateway_process = lambda **_kwargs: None
    daemon.serve_ops_daemon = ops_run
    daemon.main(argv=[])


def main(started: Path, marker: Path, mode: str) -> None:
    install_graceful_shutdown("private-service-test")

    background: set[asyncio.Task[None]] = set()

    async def run() -> None:
        try:
            if mode == "ops-runner":
                entered = threading.Event()

                def stuck_worker() -> None:
                    entered.set()
                    threading.Event().wait()

                background.add(asyncio.create_task(asyncio.to_thread(stuck_worker)))
                while not entered.is_set():
                    await asyncio.sleep(0.01)
            started.write_text("ready")
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.05)
            marker.write_text("clean")

    if mode == "ops-runner":
        run_ops_runner_entry(run)
    else:
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(run())


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3])
