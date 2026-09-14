"""Entry point: wire a supervisor and a control server, own the process.

Run: `python -m services.ava_root --run-dir DIR --manifests FILE [--wiring module:attr]`

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
  then exit 0; SIGHUP is ignored (no reloads — reloads go through upgrade).
- **Upgrade**: `upgrade` (K1) makes this daemon exec-replace itself — same
  pid, same argv, environment carrying the takeover marker — once the accepted
  response is flushed. The successor validates `<run-dir>/handoff.json`
  against its own pid (exec keeps the pid), attaches the still-running units
  without respawning them, and rebinds the control socket. Between the exec
  and the rebind, K1 refuses connections for well under a second. A failed
  handoff write is answered as an error (no exec happens); an exec failure
  exits 1.
- **Exit codes**: 0 = clean stop; 1 = startup refused (malformed manifests, a
  broken wiring reference, or another live supervisor already owns the run dir
  — a keeper launching into a held tree must read that as "already running",
  not as a crash) or an upgrade exec failure (the launch edge then restarts a
  cold generation).
- **Run-dir layout**: `ava-root.lock` (instance lock, held for the process
  lifetime), `ava-root.sock` (control socket, mode 0600),
  `logs/<unit>/output.log` (per-unit directory; output appended across
  generations, the directory is the seam for naming/rotation policy).
- **Wiring hook (optional)**: `--wiring module:attr` imports a
  deployment-side module and calls its attribute once with a `WiringContext`;
  the returned participant(s) start with the tree and stop (reverse order)
  before it. Without the flag the daemon imports nothing extra and behaves
  exactly as before. Contract: `services/ava_root/wiring.py`.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from services.ava_root.handoff import discard_handoff, load_handoff
from services.ava_root.ipc import RequestPayload, Verb
from services.ava_root.manifest import ManifestError, load_manifests
from services.ava_root.server import ControlServer
from services.ava_root.singleton import (
    AlreadyRunningError,
    acquire_instance_lock,
    release_instance_lock,
)
from services.ava_root.supervisor import Supervisor
from services.ava_root.wiring import (
    WiringContext,
    WiringError,
    WiringParticipant,
    load_wiring,
    start_participants,
    stop_participants,
)
from shared.process_env import consume_process_marker, inherited_process_env

_log = logging.getLogger("ava_root")

_SOCKET_NAME = "ava-root.sock"


@dataclass(frozen=True, slots=True)
class DaemonOptions:
    """Parsed command line of the daemon."""

    run_dir: Path
    manifests_path: Path
    wiring: str | None = None


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
    parser.add_argument(
        "--wiring",
        default=None,
        help="optional 'module:attribute' wiring hook, driven with the daemon lifecycle",
    )
    parsed = parser.parse_args(argv)
    return DaemonOptions(
        run_dir=parsed.run_dir,
        manifests_path=parsed.manifests,
        wiring=parsed.wiring,
    )


# The exec handoff marker: the one piece of process state that must cross the
# `execve` boundary of an in-place upgrade. It rides the environment through
# `shared.process_env` — the seam built for exactly this class (Settings is
# per-process configuration loaded at import time; this marker lives only
# between one generation's exec and its successor's startup).
_HANDOFF_ENV = "AVA_ROOT_HANDOFF"


def _take_takeover_marker() -> bool:
    """Consume the exec-handoff marker this process was launched with, if any."""
    return consume_process_marker(_HANDOFF_ENV, armed_value="1")


def _takeover_env() -> dict[str, str]:
    """The environment for a replacement generation: this one plus the marker."""
    return inherited_process_env({_HANDOFF_ENV: "1"})


def _exec_upgrade(exec_args: Sequence[str]) -> NoReturn:
    """Replace this process image with a fresh generation (same pid, same args).

    Called only after the accepted upgrade response has been flushed. On
    success this never returns — `execve` keeps the pid, the parent, and the
    inherited children, so the successor attaches the same units. An exec
    failure exits the process with status 1: the launch edge's keepalive then
    starts a cold generation (resolving the old tree's fate is G4's recovery
    territory, not silently re-adopted here).
    """
    argv = [sys.executable, "-m", "services.ava_root", *exec_args]
    _log.info("upgrade: exec replacement now (pid stays %s)", os.getpid())
    try:
        os.execve(  # noqa: S606 — replaces this process image with the same trusted interpreter
            sys.executable, argv, _takeover_env()
        )
    except OSError as exc:
        _log.error(
            "upgrade: exec failed: %s — exiting; the launch edge restarts a cold generation",
            exc,
        )
        logging.shutdown()
        os._exit(1)
    os._exit(1)  # unreachable: execve replaces the image or raises


async def run(options: DaemonOptions, *, exec_args: Sequence[str]) -> int:
    """Bring up one tree and serve until a stop signal arrives."""
    registry = load_manifests(options.manifests_path)
    lock_fd = acquire_instance_lock(options.run_dir)
    takeover = _take_takeover_marker()
    handoff = load_handoff(
        options.run_dir, expected_writer_pid=os.getpid(), takeover_marker=takeover
    )
    log_dir = options.run_dir / "logs"
    supervisor = Supervisor(registry, run_dir=options.run_dir)
    context = WiringContext(
        supervisor=supervisor,
        registry=registry,
        run_dir=options.run_dir,
        log_dir=log_dir,
    )
    # Fail-fast, before any tree state exists: a broken wiring reference must
    # refuse startup, not leave a half-wired daemon behind.
    participants = load_wiring(options.wiring, context)
    socket_path = options.run_dir / _SOCKET_NAME
    stop = asyncio.Event()

    def after_response(request: RequestPayload) -> None:
        """Exec exactly once the accepted upgrade response is flushed."""
        if request.get("verb") != Verb.UPGRADE.value:
            return
        if not supervisor.take_pending_upgrade():
            return
        if stop.is_set():
            _log.warning("upgrade exec skipped: the daemon is already shutting down")
            return
        _exec_upgrade(exec_args)

    server = ControlServer(socket_path, supervisor.dispatch, after_response=after_response)
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    # SIGHUP does not reload anything; ignoring it keeps a stray terminal
    # hangup from killing the tree (reloads go through the upgrade protocol).
    loop.add_signal_handler(signal.SIGHUP, lambda: _log.info("SIGHUP ignored"))
    started: list[WiringParticipant] = []
    try:
        await supervisor.start(handoff=handoff)
        if handoff is not None:
            discard_handoff(options.run_dir)
        await server.start()
        started = await start_participants(participants)
        if started:
            _log.info(
                "ava-root ready: %d unit(s), socket %s, %d wired participant(s)",
                len(registry.units),
                socket_path,
                len(started),
            )
        else:
            _log.info(
                "ava-root ready: %d unit(s), socket %s",
                len(registry.units),
                socket_path,
            )
        await stop.wait()
        _log.info("stop signal received; stopping the tree")
    finally:
        # Participants first: their loops touch the tree, so they stop while it
        # (and the control server) still exist. A failing stop never blocks the
        # tree shutdown below.
        await stop_participants(started)
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
    tokens = list(sys.argv[1:] if argv is None else argv)
    options = parse_args(tokens)
    try:
        return asyncio.run(run(options, exec_args=tokens))
    except (ManifestError, AlreadyRunningError, WiringError) as exc:
        _log.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
