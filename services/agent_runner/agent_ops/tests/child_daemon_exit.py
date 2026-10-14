"""Child entry for the ops daemon's process-exit tests.

Usage: `child_daemon_exit.py <scenario> [args]`, run in a fresh interpreter
because each scenario ends in the shared `_hard_exit` and never returns.
"""

import pathlib
import sys
import time

from base.config import ConfigBoot
from base.config.service_read import ConfigAuthority
from services.agent_runner.agent_ops import boot, daemon


def _crash() -> None:
    async def _boom(
        *, database: object, image: object, local_machine: object, config: object, host: object
    ) -> None:
        raise RuntimeError("db pool exploded mid-loop")

    def _silent(*_args: object, **_kwargs: object) -> None:
        return None

    daemon.init_gateway_process = _silent
    daemon.install_graceful_shutdown = _silent
    daemon.serve_ops_daemon = _boom
    sys.argv[1:] = []  # the daemon parses its own argv
    daemon.main()


def _wedged_arm(home: pathlib.Path, ready: pathlib.Path) -> None:
    config = ConfigBoot()
    config.boot()
    runtime = config.ensure_eager()
    authority = ConfigAuthority(
        runtime,
        runtime if runtime.profile is None else config.complete_read_model(),
        home / ".env",
    )
    pool = boot.op_thread_pool(authority=authority)
    pool.submit(lambda: (ready.write_text("1"), time.sleep(3600)))
    while not ready.exists():
        time.sleep(0.01)
    boot.shutdown_op_pool(pool)
    daemon._hard_exit(0)


if __name__ == "__main__":
    if sys.argv[1] == "crash":
        _crash()
    elif sys.argv[1] == "wedged_arm":
        _wedged_arm(pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3]))
    elif sys.argv[1] == "hard_exit":
        daemon._hard_exit(int(sys.argv[2]))
    else:
        raise SystemExit(f"unknown scenario {sys.argv[1]!r}")
