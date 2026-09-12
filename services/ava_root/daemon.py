"""Entry point: wire a supervisor and a control server, own the process.

Run: `python -m services.ava_root --run-dir DIR --manifests FILE`

The daemon owns exactly one tree (the instance lock enforces it), serves the
K1 control protocol on `<run-dir>/ava-root.sock`, and stops the whole tree on
SIGTERM/SIGINT. What launches *this* process (the OS edge) is out of scope —
this program is deliberately launchable by hand, by a test, or later by the
platform adapter.

Process contract for an OS-edge launcher (the K3 face this program freezes):

- **Launch**: `python -m services.ava_root --run-dir DIR --manifests FILE`.
  The run directory is created when missing.
- **Ready**: the control socket appearing on disk — the server starts only
  after the tree is up.
- **Stop**: SIGTERM (or SIGINT) triggers the graceful stop of the whole tree,
  then exit 0. SIGHUP is currently ignored.
- **Exit codes**: 0 = clean stop; 1 = startup refused (malformed manifests, or
  another live supervisor already owns the run dir — a keeper launching into a
  held tree must read that as "already running", not as a crash).
- **Run-dir layout**: `ava-root.lock` (instance lock, held for the process
  lifetime), `ava-root.sock` (control socket, mode 0600), `logs/<unit>.log`
  (per-unit output, appended across generations).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from services.ava_root.manifest import ManifestError, load_manifests
from services.ava_root.server import ControlServer
from services.ava_root.singleton import (
    AlreadyRunningError,
    acquire_instance_lock,
    release_instance_lock,
)
from services.ava_root.supervisor import Supervisor

_log = logging.getLogger("ava_root")

_SOCKET_NAME = "ava-root.sock"


@dataclass(frozen=True, slots=True)
class DaemonOptions:
    """Parsed command line of the daemon."""

    run_dir: Path
    manifests_path: Path


def parse_args(argv: Sequence[str] | None = None) -> DaemonOptions:
    """Parse the daemon command line."""
    parser = argparse.ArgumentParser(
        prog="ava-root",
        description="Platform-neutral root supervisor: one process tree per (machine x home).",
    )
    parser.add_argument(
        "--run-dir",
        required=True,
        type=Path,
        help="directory for the control socket, the instance lock and unit logs",
    )
    parser.add_argument(
        "--manifests",
        required=True,
        type=Path,
        help="unit manifest JSON file",
    )
    parsed = parser.parse_args(argv)
    return DaemonOptions(run_dir=parsed.run_dir, manifests_path=parsed.manifests)


async def run(options: DaemonOptions) -> int:
    """Bring up one tree and serve until a stop signal arrives."""
    registry = load_manifests(options.manifests_path)
    lock_fd = acquire_instance_lock(options.run_dir)
    supervisor = Supervisor(registry, log_dir=options.run_dir / "logs")
    socket_path = options.run_dir / _SOCKET_NAME
    server = ControlServer(socket_path, supervisor.dispatch)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    # SIGHUP does not reload anything yet; ignoring it keeps a stray terminal
    # hangup from killing the tree before the upgrade protocol exists.
    loop.add_signal_handler(signal.SIGHUP, lambda: _log.info("SIGHUP ignored"))
    try:
        await supervisor.start()
        await server.start()
        _log.info(
            "ava-root ready: %d unit(s), socket %s",
            len(registry.units),
            socket_path,
        )
        await stop.wait()
        _log.info("stop signal received; stopping the tree")
    finally:
        await server.close()
        await supervisor.shutdown()
        release_instance_lock(lock_fd)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Daemon entry point; returns the process exit code."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    options = parse_args(argv)
    try:
        return asyncio.run(run(options))
    except (ManifestError, AlreadyRunningError) as exc:
        _log.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
