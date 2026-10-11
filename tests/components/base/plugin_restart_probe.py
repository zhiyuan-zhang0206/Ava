"""Child process for the plugin-restart shutdown test.

Usage: `plugin_restart_probe.py <marker path>`. The agent host's plugin watcher sees a fingerprint
change and the process is then stopped; the marker file proves the product handler's cleanup ran.
"""

import asyncio
import contextlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from base.daemon.shutdown import install_graceful_shutdown
from services.agent_runner.agent_host import daemon

install_graceful_shutdown("private-plugin-restart-test")
fingerprints = iter(["before", "after"])
daemon._plugins_fingerprint = lambda: next(fingerprints)
daemon._PLUGINS_POLL_INTERVAL_S = 0.01


async def run() -> None:
    try:
        await daemon._watch_plugins_for_restart()
    finally:
        await asyncio.sleep(0.05)
        Path(sys.argv[1]).write_text("clean")


with contextlib.suppress(KeyboardInterrupt):
    asyncio.run(run())
