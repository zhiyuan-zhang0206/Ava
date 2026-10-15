import asyncio
import contextlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from base.daemon.shutdown import install_graceful_shutdown
from services.agent_runner.agent_host import daemon

install_graceful_shutdown("private-plugin-restart-test")
fingerprints = iter(["before", "after"])
daemon.plugins_fingerprint = lambda: next(fingerprints)
daemon.PLUGINS_POLL_INTERVAL_S = 0.01


async def run() -> None:
    try:
        await daemon.watch_plugins_for_restart()
    finally:
        await asyncio.sleep(0.05)
        Path(sys.argv[1]).write_text("clean")


with contextlib.suppress(KeyboardInterrupt):
    asyncio.run(run())
