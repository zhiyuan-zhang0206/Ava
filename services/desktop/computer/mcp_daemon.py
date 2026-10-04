"""Per-machine shared computer-use MCP service.

One daemon executes every desktop action on this machine — the executor layer
of the computer-use capability (task #1101). It sits between agents and the
desktop:

- Every action goes through the signed permissions helper
  (`services.desktop.permissions_helper.client`), the only process holding the macOS
  TCC screen-recording / accessibility grants — no new code path ever touches
  CGEvent / screencapture directly.
- Actions are serialized machine-wide (one asyncio lock around execute): the
  desktop is one shared screen, and a snapshot's multi-step capture never
  interleaves with another agent's click (same serial choice the browser-mcp
  daemon made for the browser).
- Every action is audited as a `computer_action` event (outcome ok / error)
  — facts for later review, nothing is refused here. Per-agent permission
  division is a prompt-level convention between peers (user ruling 2026-08-10),
  not code-enforced governance; the cluster's security boundary is its entry
  point, not this daemon.
- Phase 2: screen ownership is coordinated (holder + renewable lease + FIFO
  queue + release_control + operator kick — `services/desktop/computer/session.py`);
  `snapshot(include_ocr=true)` adds Vision OCR text boxes
  (`services/desktop/computer/ocr.py`); task_id calls get a computer_session_start/end
  envelope (`services/desktop/computer/task_sessions.py`).
- OCR text tools (task #2401): `find_text` locates recognized text and
  returns its physical-pixel boxes; `click_text` OCRs, locates, and clicks in
  one serialized action — the same Vision OCR `snapshot(include_ocr=true)`
  exposes, wrapped so callers act on what they read on screen.

Module map: `screen.py` owns capture and coordinate translation;
`ocr_text.py` owns OCR and text tools; `execute.py` owns the MCP declarations
and per-tool execution; `errors.py` owns the shared tool error. This module
keeps the daemon lifecycle, serialization, screen ownership, and auditing.

Wire protocol (JSON line per request, mirrors `services/desktop/browser/protocol`):
  Request:  {"id": 1, "method": "ping"}
            {"id": 2, "method": "call_tool", "tool": "click", "args": {...}, "agent_id": 42}
  Response: {"id": 1, "ok": true,  "result": "pong"}
            {"id": 2, "ok": true,  "result": {"content": [{"type": "text", "text": "..."}], "isError": false}}
            # call_tool result: an MCP CallToolResult dump — the tool's plain
            # dict rides as one JSON text block (the contract both the
            # per-agent wrapper and the direct dial validate against)
            {"id": 2, "ok": false, "error": "message"}

The per-agent bridge (`services/desktop/computer/mcp_wrapper.py`) and the MCP daemon's
direct dial (`ava/mcps/_computer.py`) speak this protocol; `agent_id` is stamped
by the bridge from the calling agent's identity and rides into the audit
stream, where it is likewise self-reported by the agent's own process.

Coordinate contract: every tool coordinate is in PHYSICAL pixels — the same
space as the `snapshot` PNG, so a caller can click exactly where it saw a
pixel. The daemon divides by the backing scale before calling the helper,
whose CGEvent space is logical points (Retina 2x on macOS; Windows reports
scale 1, so the conversion is a no-op there). The scale is measured from the
capture (PNG physical size vs logical screen size), not taken from the helper
report — the helper holds no AppKit event loop and can serve a stale scale
(2026-08-30: scale 2 on a 1x display, halving every click). `snapshot` returns
the physical pixel size and the logical screen size + the measured scale.

Run as a supervised daemon (ServiceSpec session "computer-mcp"):
    .venv/bin/python -m services.desktop.computer.mcp_daemon
"""

from __future__ import annotations

import asyncio
import faulthandler
import json
import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from functools import partial
from pathlib import Path
from typing import Any

from base.config import settings
from base.db import Database
from base.log import logger
from base.paths import computer_mcp_socket
from base.telemetry import audit_events

# Re-export of the shared OCR module object (test compat: the suite patches
# mcp_daemon.ocr_mod attributes, and every OCR caller sees the same object).
from services.desktop.computer.ax_ids import AxSession
from services.desktop.computer.config import ComputerUseConfig
from services.desktop.computer.execute import _TOOLS, _execute_tool, _mcp_result, _priority
from services.desktop.computer.execute import ocr_mod as ocr_mod
from services.desktop.computer.protocol import Request, Response
from services.desktop.computer.session import ScreenSession
from services.desktop.computer.task_sessions import TaskSessionTracker
from services.desktop.permissions_helper import client as helper
from services.desktop.permissions_helper.client import PermissionsHelperError

# A snapshot PNG can be multi-MB on one line; lift the stream buffer cap well
# above StreamReader's 64KiB default (same limit as the browser daemon).
_LINE_LIMIT = 64 * 1024 * 1024


def _audit_coords(tool: str, args: dict[str, Any], result: dict[str, Any] | None) -> str | None:
    """The compact "where / what" string of a computer_action audit row."""
    if tool == "click" and "x" in args:
        return f"{args['x']},{args['y']}"
    if tool == "click_text" and result is not None:
        # click_text resolves its own target via OCR: audit the center it
        # clicked (physical pixels), not an argument coordinate.
        return f"{result.get('x')},{result.get('y')}"
    if tool == "ax_act" and result is not None:
        # The element's center and the action — never the value written.
        return f"{result.get('x')},{result.get('y')},{result.get('action')}"
    if tool == "scroll":
        return f"{args.get('x')},{args.get('y')},{args.get('dy')}"
    if tool == "key":
        return str(args.get("key") or args.get("keycode") or "")
    return None


def computer_use_config() -> ComputerUseConfig:
    """The composition root: the one place this package reads `settings`."""
    return ComputerUseConfig(
        computer_use_lease_s=settings.daemon.computer_use_lease_s,
        computer_use_queue_timeout_s=settings.daemon.computer_use_queue_timeout_s,
        computer_use_session_idle_s=settings.daemon.computer_use_session_idle_s,
        computer_use_loop_stall_s=settings.daemon.computer_use_loop_stall_s,
        computer_use_shutdown_drain_s=settings.daemon.computer_use_shutdown_drain_s,
    )


# ── loop-liveness watchdog ──────────────────────────────────────────────────
# Tools execute synchronously on the event loop (one action at a time
# machine-wide). A sync call that never returns therefore wedges the whole
# daemon: it stops accepting connections, cannot answer the healthcheck's
# ping, and cannot even run its SIGTERM handler (Python only reaches that
# handler through the loop). The 2026-10-04 incident: a wedged loop left the
# unit unstoppable for ~15 minutes until a manual kill, because a blocked
# loop cannot exit for the supervisor to respawn it, and the root stop
# window (10s) passed five times into the restart breaker. The watchdog
# bounds the blocked-loop half of that failure class: the loop re-beats a
# timestamp on every tick and a background thread checks it; when the loop
# has not beaten for the configured window, the watchdog dumps every
# thread's stack to stderr (captured into the unit's output.log by the
# supervisor) and exits(1) — a dead process the supervisor restarts on its
# normal path. The incident's other half, a shutdown that never finished, is
# bounded by `_bounded_cleanup` below. Either serving, or gone.
_WATCHDOG_BEAT_S = 15.0  # loop-side re-beat interval (watchdog sampling detail)
_WATCHDOG_CHECK_S = 5.0  # watchdog-thread check interval (watchdog sampling detail)
_WATCHDOG_MIN_STALL_S = 1.0  # below this a misconfigured window would restart a healthy daemon


class _LoopWatchdog(threading.Thread):
    """Run `on_stall` once the event loop has not beaten for `stall_s`.

    `on_stall` executes on this thread and must end the process (production:
    `_dump_and_exit`); when it returns, the watchdog ends. The only beat source
    is the loop's own tick callback, so a blocked loop stops beating — while
    a live loop keeps the watchdog silent no matter how long a *caller* waits.
    """

    def __init__(
        self,
        stall_s: float,
        *,
        check_s: float,
        on_stall: Callable[[], None],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(name="computer-mcp-loop-watchdog", daemon=True)
        self._stall_s = stall_s
        self._check_s = check_s
        self._on_stall = on_stall
        self._clock = clock
        self._beat = clock()
        self._stopped = threading.Event()

    def beat(self) -> None:
        """Prove the loop is alive (called from the loop's tick callback)."""
        self._beat = self._clock()

    def stale_for(self) -> float:
        """Seconds since the last beat."""
        return self._clock() - self._beat

    @property
    def stopped(self) -> bool:
        return self._stopped.is_set()

    def stop(self) -> None:
        self._stopped.set()

    def run(self) -> None:
        while not self._stopped.wait(self._check_s):
            if self.stale_for() > self._stall_s:
                self._on_stall()
                return


def _dump_and_exit(reason: str) -> None:
    """The last resort of every bound in this module: dump every thread's
    stack, then exit for a supervisor restart.

    Anything that reaches here passed its configured bound while the unit was
    still not serving; the only exit left that the supervisor can act on is a
    dead process. Raw stderr writes, not logging: the log path may be what is
    blocked. The supervisor captures stderr into the unit's output.log, so
    the dump — the forensics the 2026-10-04 wedge lacked — survives the exit.
    `reason` names the bound that tripped, so the first line of the dump
    already says which one it was.
    """
    try:
        sys.stderr.write(
            f"[computer-mcp] {reason} — thread stacks follow; exiting for a supervisor restart\n"
        )
        sys.stderr.flush()
        faulthandler.dump_traceback(all_threads=True)
    finally:
        os._exit(1)


def _guard_loop_liveness(
    loop: asyncio.AbstractEventLoop,
    stall_s: float,
    *,
    beat_s: float = _WATCHDOG_BEAT_S,
    check_s: float = _WATCHDOG_CHECK_S,
    on_stall: Callable[[], None] | None = None,
) -> _LoopWatchdog:
    """Start the loop-liveness watchdog and keep it beating from `loop`.

    Returns the watchdog so the shutdown path can stop it. `on_stall`
    defaults to `_dump_and_exit`; tests inject a recorder instead of the exit.
    The re-beat cadence stays well inside the window (`beat_s` shrunk to a
    third of it when the configured window is small), so an operator tuning
    the window down cannot trip a healthy loop; a tiny window is floored so
    a misconfiguration cannot restart-loop the daemon.
    """
    stall_s = max(stall_s, _WATCHDOG_MIN_STALL_S)
    watchdog = _LoopWatchdog(
        stall_s,
        check_s=check_s,
        on_stall=on_stall or partial(_dump_and_exit, f"run loop silent for over {stall_s}s"),
    )
    watchdog.start()
    beat_interval = min(beat_s, stall_s / 3)

    def tick() -> None:
        if watchdog.stopped:
            return
        watchdog.beat()
        loop.call_later(beat_interval, tick)

    tick()
    return watchdog


async def _bounded_cleanup(
    server: asyncio.AbstractServer, clients: set[asyncio.Task[None]], *, drain_s: float
) -> None:
    """Close out the clients and the listener, bounded by `drain_s`.

    `server.close()` has already closed the listening socket; `wait_closed()`
    returns only once every handler task is done, and a client holding its
    connection open (the SDK keeps one persistent socket) or a handler stuck
    in its own unwind would otherwise hang shutdown forever — the second half
    of the 2026-10-04 wedge: listener closed (every later connect refused,
    `Errno 61`), the stop signal already consumed, and the process neither
    serving nor dying until a manual kill.

    The clients wait is `asyncio.wait`, not `gather`, and deliberately so:
    `gather`'s `cancel()` forwards into its children and leaves the awaiting
    task parked on a future that never completes (CPython #32684 semantics), so
    a `gather`-based drain could not be un-parked by any timeout — the bound
    must be structural. `wait`'s own timer completes the wait regardless of its
    children; a handler that ignores cancellation is reported by `wait` as
    still pending, never awaited again. Past the bound there is no clean path
    left inside the process, so it dumps every thread's stack and exits for a
    supervisor restart — the same last resort as the loop watchdog.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + drain_s
    pending = list(clients)
    for task in pending:
        task.cancel()
    if pending:
        _, still_pending = await asyncio.wait(pending, timeout=drain_s)
        if still_pending:
            _dump_and_exit(f"shutdown cleanup did not finish within {drain_s:g}s")
    try:
        async with asyncio.timeout(max(deadline - loop.time(), 0.0)):
            await server.wait_closed()
    except TimeoutError:
        _dump_and_exit(f"shutdown cleanup did not finish within {drain_s:g}s")


class ComputerMcpDaemon:
    """Unix-socket server for the computer-mcp line protocol."""

    def __init__(self, config: ComputerUseConfig, db: Database, sock: str | None = None) -> None:
        self._db = db
        self._sock = sock or str(computer_mcp_socket())
        # Last pointer position in PHYSICAL pixels (set by click / explicit
        # scroll); the scroll fallback when the caller gives no x/y.
        self._pointer: tuple[float, float] | None = None
        # Last measured physical->logical scale (snapshot PNG vs logical size).
        # None until the first snapshot; click/scroll use it via _current_scale.
        self._scale: float | None = None
        # Last OCR text boxes ({items: [...]}) — refreshed by every OCR the
        # daemon runs (snapshot include_ocr, find_text, click_text) so a later
        # find_text with snapshot_fresh=false searches the screen the caller
        # last saw instead of capturing again.
        self._ocr_cache: dict[str, Any] = {"items": []}
        # Stable accessibility element ids (ax_tree / ax_act), one app at a time.
        self._ax_session = AxSession()
        # One lock around execute: a single desktop op at a time machine-wide,
        # and a snapshot's multi-step capture never interleaves with another
        # agent's click (same serial choice as browser-mcp).
        self._action_lock = asyncio.Lock()
        # Phase 2 session coordination: who owns the screen + FIFO waiters.
        self._screen = ScreenSession(
            lease_s=config.computer_use_lease_s,
            queue_timeout_s=config.computer_use_queue_timeout_s,
        )
        # Phase 2 audit envelope: task_id -> computer_session_start/end.
        self._task_sessions = TaskSessionTracker(idle_s=config.computer_use_session_idle_s)
        # Active client handler tasks, so shutdown can close them instead of
        # hanging in server.wait_closed() behind a client that never disconnects
        # (the pre-fix orphan: SIGTERM left the process alive holding the socket).
        self._clients: set[asyncio.Task[None]] = set()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            with suppress(ConnectionResetError, BrokenPipeError):
                while True:
                    line = await reader.readline()
                    if not line:
                        break
                    try:
                        req: Request = json.loads(line)
                    except json.JSONDecodeError as e:
                        resp: Response = {
                            "id": None,
                            "ok": False,
                            "error": f"JSON parse error: {e}",
                        }
                        writer.write((json.dumps(resp) + "\n").encode())
                        await writer.drain()
                        continue
                    resp = await self._dispatch(req)
                    writer.write((json.dumps(resp, ensure_ascii=False) + "\n").encode())
                    await writer.drain()
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()

    async def _dispatch(self, req: Request) -> Response:
        req_id = req.get("id")
        if not isinstance(req_id, int) or isinstance(req_id, bool):
            return {"id": None, "ok": False, "error": "request id must be an integer"}
        method = req.get("method")
        if method == "ping":
            return {"id": req_id, "ok": True, "result": "pong"}
        if method == "list_tools":
            return {"id": req_id, "ok": True, "result": _TOOLS}
        if method != "call_tool":
            return {"id": req_id, "ok": False, "error": f"unknown method {method!r}"}
        tool = req.get("tool") or ""
        args = req.get("args") or {}
        agent_id = req.get("agent_id")
        return await self._call_tool(tool, args, agent_id, req_id)

    def _track_tool_effects(self, tool: str, args: dict[str, Any], result: dict[str, Any]) -> None:
        """Remember the scale and pointer the tool's result established, for later conversions."""
        if tool == "snapshot":
            # click/scroll convert with the scale the caller saw.
            self._scale = float(result["screen"]["scale"])
        elif tool == "click_text":
            # The click landed where OCR found the text; record the
            # pointer AND the scale its capture measured, so later
            # click/scroll convert like this call did.
            self._scale = float(result["scale"])
            self._pointer = (float(result["x"]), float(result["y"]))
        if tool == "click" or (tool == "scroll" and "x" in args and "y" in args):
            self._pointer = (float(args["x"]), float(args["y"]))

    async def _renew_and_note(self, agent_id: int, tool: str, args: dict[str, Any]) -> None:
        """A live caller renews the lease (success or failure — it is still acting on the
        screen) and its task session notes the action."""
        await self._screen.touch(agent_id)
        task_id = args.get("task_id")
        if task_id is None:
            return
        # Garbage task_id: the computer_action row still carries it
        # as-is; the envelope just does not form (suppress, not a
        # silent except:pass — the action is what matters here).
        tid: int | None = None
        with suppress(TypeError, ValueError):
            tid = int(task_id)
        if tid is None:
            return
        try:
            self._task_sessions.note(tid, agent_id, tool, self._emit_session_event)
        except Exception as e:  # auxiliary path, see below
            # The session envelope is auxiliary to the action
            # itself: never fail the action, but never stay
            # silent either — a contract mismatch (unregistered
            # event name, FK hiccup) must be audible.
            logger.warning(f"[computer-mcp] task-session event failed: {e}")

    async def _call_tool(
        self, tool: str, args: dict[str, Any], agent_id: int | None, req_id: int
    ) -> Response:
        if tool == "release_control":
            return await self._release_control(agent_id, args, req_id)
        if agent_id is not None and not await self._screen.acquire(
            agent_id, priority=_priority(args)
        ):
            # Someone else holds the screen and did not let go in time. Fail
            # with a readable busy error instead of interleaving actions.
            return {
                "id": req_id,
                "ok": False,
                "error": f"screen busy: held by agent {self._screen.holder or '?'} — "
                "wait for release_control or the lease to expire, then retry",
            }
        async with self._action_lock:
            outcome = "ok"
            error: str | None = None
            try:
                result = _execute_tool(
                    tool,
                    args,
                    agent_id or 0,
                    pointer=self._pointer,
                    scale=self._scale,
                    ocr_cache=self._ocr_cache,
                    ax_session=self._ax_session,
                )
                self._track_tool_effects(tool, args, result)
            except (PermissionsHelperError, KeyError, TypeError, ValueError, OSError) as e:
                outcome, error = "error", f"{type(e).__name__}: {e}"
                result = None
            except Exception as e:  # unknown failure: audit + surface, stay alive
                outcome, error = "error", f"{type(e).__name__}: {e}"
                result = None
            if agent_id is not None:
                await self._renew_and_note(agent_id, tool, args)
            self._emit_action(agent_id, tool, args, outcome, error, result=result)
            if error is not None:
                return {"id": req_id, "ok": False, "error": error}
            assert result is not None  # noqa: S101 — no error ⇒ execution succeeded
            return {"id": req_id, "ok": True, "result": _mcp_result(result)}

    async def _release_control(
        self, agent_id: int | None, args: dict[str, Any], req_id: int
    ) -> Response:
        """release_control tool: the holder hands the screen to the next FIFO
        waiter. `force` (CLI-only, not in the MCP schema) releases regardless of
        who holds it — the operator's last resort for a wedged session."""
        async with self._action_lock:
            released = await self._screen.release(None if args.get("force") else agent_id)
            outcome = "ok" if released is not None else "error"
            error = None if released is not None else "not the screen holder"
            if args.get("force") and released is not None:
                # Operator kick: no agent identity, so no audit row — log it.
                logger.info(f"[computer-mcp] operator forced release of agent {released}")
            self._emit_action(agent_id, "release_control", args, outcome, error)
            if released is None:
                return {"id": req_id, "ok": False, "error": "not the screen holder"}
            return {
                "id": req_id,
                "ok": True,
                "result": _mcp_result({"released": released, "holder": self._screen.holder}),
            }

    def _emit_action(
        self,
        agent_id: int | None,
        tool: str,
        args: dict[str, Any],
        outcome: str,
        error: str | None,
        result: dict[str, Any] | None = None,
    ) -> None:
        """One computer_action audit event per call — facts for later review."""
        coords = _audit_coords(tool, args, result)
        if agent_id is None:
            # No identity, no audit row: events.agent_id references agents(id).
            return
        app = None
        # Enrichment only: an unreachable helper is already reported by the
        # tool call itself.
        with suppress(PermissionsHelperError, OSError):
            app = helper.frontmost_app()["app"] or None
        event = audit_events.prepare_event_log(
            event_type="computer_action",
            agent_id=agent_id,
            source=f"agent:{agent_id}",
            payload={
                "action": tool,
                "app": app,
                "outcome": outcome,
                "error": error,
                "coords": coords,
                "path": result.get("path") if tool == "snapshot" and result else None,
                "task_id": args.get("task_id"),
            },
        )
        from base.agents.impersonation_manifest import emit_recorded_central_event

        emit_recorded_central_event(self._db, event)

    def _emit_session_event(self, event_type: str, agent_id: int, payload: dict[str, Any]) -> None:
        """One computer_session_start/end audit row (no app lookup — the
        envelope describes the task, not a screen state)."""
        event = audit_events.prepare_event_log(
            event_type=event_type,
            agent_id=agent_id,
            source=f"agent:{agent_id}",
            payload=payload,
        )
        from base.agents.impersonation_manifest import emit_recorded_central_event

        emit_recorded_central_event(self._db, event)


async def _socket_in_use(path: Path) -> bool:
    """True when a live process is already listening on ``path``.

    A successful connect proves an occupant; ``FileNotFoundError`` /
    ``ConnectionRefusedError`` mean a stale socket (nobody listening) and are
    safe to unlink. Any other error is treated as occupied — fail closed
    rather than risk stealing a live instance's socket (same guard as
    browser-mcp; without it a second daemon spawned by a watchdog that
    misjudged the first dead steals the socket and orphans the live one).
    """
    try:
        reader, writer = await asyncio.open_unix_connection(path=path)
    except (FileNotFoundError, ConnectionRefusedError):
        return False
    except OSError:
        return True
    writer.close()
    with suppress(OSError):
        await writer.wait_closed()
    del reader  # nothing to close on a StreamReader; the writer close suffices
    return True


async def _serve_client(
    daemon: ComputerMcpDaemon, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """Run one client connection (the tracked task's coroutine).

    A client's failure ends its connection, never the daemon the clients' group lives in.
    """
    try:
        await daemon.handle(reader, writer)
    except Exception:
        logger.exception("[computer-mcp] client handler failed")


def _tracked_client(
    daemon: ComputerMcpDaemon,
    clients: asyncio.TaskGroup,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> asyncio.Task[None]:
    """Spawn the per-connection handler as a tracked task (cancellable at shutdown)."""
    task = clients.create_task(_serve_client(daemon, reader, writer))
    daemon._clients.add(task)
    task.add_done_callback(daemon._clients.discard)
    return task


async def run(sock: str | None = None) -> None:
    config = computer_use_config()
    daemon = ComputerMcpDaemon(config, Database.from_settings(), sock)
    path = Path(daemon._sock)
    if await _socket_in_use(path):
        logger.error(
            "[computer-mcp] socket {} is already served by a live daemon — "
            "refusing to start a second instance",
            path,
        )
        raise SystemExit(1)
    with suppress(OSError):
        path.unlink()
    # The group owns every client handler; the shutdown path cancels them before it closes.
    async with asyncio.TaskGroup() as clients:
        server = await asyncio.start_unix_server(
            lambda r, w: _tracked_client(daemon, clients, r, w),
            path=str(path),
            limit=_LINE_LIMIT,
        )
        logger.info(f"[computer-mcp] listening on {path}")
        watchdog = _guard_loop_liveness(
            asyncio.get_running_loop(), config.computer_use_loop_stall_s
        )
        stop = asyncio.Event()

        def _stop() -> None:
            stop.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            with suppress(NotImplementedError):
                asyncio.get_running_loop().add_signal_handler(sig, _stop)
        try:
            await stop.wait()
        finally:
            watchdog.stop()
            server.close()
            await _bounded_cleanup(
                server, daemon._clients, drain_s=config.computer_use_shutdown_drain_s
            )
            with suppress(OSError):
                path.unlink()
            logger.info("[computer-mcp] shutting down")


def main() -> None:
    from base.log import init_gateway_process

    init_gateway_process(name="computer-mcp")
    asyncio.run(run())


if __name__ == "__main__":
    main()
