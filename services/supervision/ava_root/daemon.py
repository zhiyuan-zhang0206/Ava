"""Launch one root-owned service tree and its control socket.

The instance lock prevents competing roots. Socket availability acknowledges
only the control plane; service readiness comes from protocol responses.
SIGTERM/SIGINT requests bounded best-effort child closure.
Release replacement stops the old root before launching another interpreter.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from base.native_process.root_control.ipc import (
    ErrorCode,
    RequestPayload,
    ResponsePayload,
    Verb,
    error_response,
    ok_response,
)
from services.supervision.ava_root.manifest import ManifestError, load_manifests
from services.supervision.ava_root.server import ControlServer
from services.supervision.ava_root.singleton import (
    AlreadyRunningError,
    acquire_instance_lock,
    release_instance_lock,
)
from services.supervision.ava_root.supervisor import Supervisor
from services.supervision.ava_root.wiring import (
    WiringContext,
    WiringError,
    WiringParticipant,
    load_wiring,
    start_participants,
    stop_participants,
)

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


async def run(options: DaemonOptions) -> int:
    """Bring up one tree and serve until a stop signal arrives."""
    registry = load_manifests(options.manifests_path)
    lock_fd = acquire_instance_lock(options.run_dir)
    async with asyncio.TaskGroup() as participant_tasks:
        log_dir = options.run_dir / "logs"
        supervisor = Supervisor(registry, run_dir=options.run_dir)
        context = WiringContext(
            supervisor=supervisor,
            registry=registry,
            run_dir=options.run_dir,
            log_dir=log_dir,
            participant_tasks=participant_tasks,
        )
        # Fail-fast, before any tree state exists: a broken wiring reference must
        # refuse startup, not leave a half-wired daemon behind.
        participants = load_wiring(options.wiring, context)
        socket_path = options.run_dir / _SOCKET_NAME
        stop = asyncio.Event()
        retiring = False

        def request_stop() -> None:
            nonlocal retiring
            retiring = True
            stop.set()

        async def dispatch(request: RequestPayload) -> ResponsePayload:
            if request["verb"] == Verb.SHUTDOWN:
                return ok_response({"shutdown_requested": True})
            if retiring and request["verb"] in {Verb.UP, Verb.RESTART, Verb.RESOURCE}:
                return error_response(
                    ErrorCode.INVALID_REQUEST, "root is stopping; admission closed"
                )
            if request["verb"] == Verb.RESOURCE:
                if "name" not in request or "payload" not in request:
                    return error_response(ErrorCode.INVALID_REQUEST, "resource fields missing")
                handler = context.resource_handlers.get(request["name"])
                if handler is None:
                    return error_response(
                        ErrorCode.INVALID_REQUEST, "resource operation unavailable"
                    )
                try:
                    return ok_response(await handler(request["payload"]))
                except RuntimeError as exc:
                    return error_response(ErrorCode.INVALID_REQUEST, str(exc))
            return await supervisor.dispatch(request)

        def after_response(request: RequestPayload) -> None:
            if request["verb"] == Verb.SHUTDOWN:
                request_stop()

        server = ControlServer(socket_path, dispatch, after_response=after_response)
        loop = asyncio.get_running_loop()

        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, request_stop)
        # SIGHUP does not reload anything; ignoring it keeps a stray terminal
        # hangup from killing the tree; release replacement is an outer-owner action.
        loop.add_signal_handler(signal.SIGHUP, lambda: _log.info("SIGHUP ignored"))
        started: list[WiringParticipant] = []
        try:
            await supervisor.start()
            await server.start()
            started = await start_participants(participants)
            _log.info(
                "ava-root ready: %d unit(s), socket %s, %d wired participant(s)",
                len(registry.units),
                socket_path,
                len(started),
            )
            await stop.wait()
        except BaseException as primary:
            try:
                await _finish(started, server, supervisor, lock_fd)
            except BaseException as cleanup:
                raise BaseExceptionGroup(
                    "root operation and teardown failed", [primary, cleanup]
                ) from None
            raise
        else:
            # Participants first: their loops touch the tree, so they stop while it
            # (and the control server) still exist. A failing stop never blocks the
            # tree shutdown below.
            await _finish(started, server, supervisor, lock_fd)
        return 0


async def _finish(
    started: list[WiringParticipant], server: ControlServer, supervisor: Supervisor, lock_fd: int
) -> None:
    """Attempt each owned teardown once, releasing the singleton even on failure."""
    failures: list[BaseException] = []
    try:
        try:
            await stop_participants(started)
        except BaseException as exc:
            failures.append(exc)
    finally:
        try:
            try:
                await server.close()
            except BaseException as exc:
                failures.append(exc)
        finally:
            try:
                try:
                    await supervisor.shutdown()
                except BaseException as exc:
                    failures.append(exc)
            finally:
                release_instance_lock(lock_fd)
    if failures:
        raise BaseExceptionGroup("root teardown failed", failures)


def main(argv: Sequence[str] | None = None) -> int:
    """Daemon entry point; returns the process exit code."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    tokens = list(sys.argv[1:] if argv is None else argv)
    options = parse_args(tokens)
    try:
        return asyncio.run(run(options))
    except (ManifestError, AlreadyRunningError, WiringError) as exc:
        _log.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
