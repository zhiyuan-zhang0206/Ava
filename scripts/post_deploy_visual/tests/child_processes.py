"""Child-process entry points for test_post_deploy_visual_check.py.

Each subcommand is one process the tests start in a fresh interpreter:

- ``hard-exit REPO_ROOT``: arms the runner's two-stage budget and then wedges the
  unwind, so the second SIGALRM must hard-exit the process.
- ``reap-tree REPO_ROOT PIDFILE``: starts a driver that starts a grandchild, then
  runs the runner's descendant reaper and returns once the driver is gone.
- ``driver PIDFILE`` / ``grandchild PIDFILE``: the playwright-driver and Chromium
  stand-ins ``reap-tree`` spawns; the grandchild records its pid in PIDFILE.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def _hard_exit(repo_root: str) -> None:
    sys.path.insert(0, repo_root)
    from scripts.post_deploy_visual import check as module

    module.HARD_EXIT_DELAY_SECONDS = 1
    signal.signal(signal.SIGALRM, module.budget_expired)
    signal.alarm(1)
    try:
        time.sleep(3600)
    except RuntimeError:
        time.sleep(3600)  # simulated wedged unwind


def _reap_tree(repo_root: str, pidfile: str) -> None:
    sys.path.insert(0, repo_root)
    from scripts.post_deploy_visual.check import kill_descendants

    driver = subprocess.Popen(  # noqa: S603 - fixed interpreter running this file
        [sys.executable, __file__, "driver", pidfile]
    )
    deadline = time.monotonic() + 10
    while not Path(pidfile).exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert Path(pidfile).exists(), "grandchild never reported its pid"
    kill_descendants()
    driver.wait(timeout=5)


def _driver(pidfile: str) -> None:
    subprocess.Popen(  # noqa: S603 - fixed interpreter running this file
        [sys.executable, __file__, "grandchild", pidfile]
    )
    time.sleep(120)


def _grandchild(pidfile: str) -> None:
    Path(pidfile).write_text(str(os.getpid()))
    time.sleep(120)


def main(argv: list[str]) -> None:
    command, *args = argv
    if command == "hard-exit":
        _hard_exit(*args)
    elif command == "reap-tree":
        _reap_tree(*args)
    elif command == "driver":
        _driver(*args)
    elif command == "grandchild":
        _grandchild(*args)
    else:
        raise SystemExit(f"unknown command {command!r}")


if __name__ == "__main__":
    main(sys.argv[1:])
