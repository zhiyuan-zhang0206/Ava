"""Child entry that runs the blocked Feishu websocket scenario in a fresh process.

Configuration comes from `DAEMON_SHUTDOWN_TEST_MARKERS` and
`FEISHU_TEST_RESPONSIVE_LOOP` in the environment.
"""

import os
from pathlib import Path

from services.entrypoints.im_bridge.tests.adapters.test_feishu_worker_exit import (
    run_blocked_ws_child,
)

if __name__ == "__main__":
    run_blocked_ws_child(
        Path(os.environ["DAEMON_SHUTDOWN_TEST_MARKERS"]),
        responsive=bool(int(os.environ["FEISHU_TEST_RESPONSIVE_LOOP"])),
    )
