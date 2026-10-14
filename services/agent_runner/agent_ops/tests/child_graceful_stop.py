"""A service for test_shutdown: the common graceful shutdown around one loop, or the ops daemon's entry.

`python child_graceful_stop.py <started file> <marker file> <asyncio-run|ops-runner>`. It writes the
started file once the loop is up and the marker file once cleanup has run after the stop.
"""

import asyncio
import contextlib
import sys
import threading
from pathlib import Path
from typing import Any

from base.daemon.shutdown import install_graceful_shutdown


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
        from services.agent_runner.agent_ops import daemon

        async def ops_run(**_kwargs: Any) -> None:
            await run()

        daemon.init_gateway_process = lambda **_kwargs: None  # pyright: ignore[reportAttributeAccessIssue]
        daemon.serve_ops_daemon = ops_run  # pyright: ignore[reportAttributeAccessIssue]
        daemon.main(argv=[])
    else:
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(run())


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3])
