"""Per-machine shared computer-use MCP service.

One daemon executes every desktop action on this machine — the executor layer
of the computer-use capability (task #1101). It sits between agents and the
desktop:

- Every action goes through the signed permissions helper
  (`services.permissions_helper.client`), the only process holding the macOS
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
  queue + release_control + operator kick — `services/computer/session.py`);
  `snapshot(include_ocr=true)` adds Vision OCR text boxes
  (`services/computer/ocr.py`); task_id calls get a computer_session_start/end
  envelope (`services/computer/task_sessions.py`).
- OCR text tools (task #2401): `find_text` locates recognized text and
  returns its physical-pixel boxes; `click_text` OCRs, locates, and clicks in
  one serialized action — the same Vision OCR `snapshot(include_ocr=true)`
  exposes, wrapped so callers act on what they read on screen.

Module map: `screen.py` owns capture and coordinate translation;
`ocr_text.py` owns OCR and text tools; `execute.py` owns the MCP declarations
and per-tool execution; `errors.py` owns the shared tool error. This module
keeps the daemon lifecycle, serialization, screen ownership, and auditing.

Wire protocol (JSON line per request, mirrors `services/browser/protocol`):
  Request:  {"id": 1, "method": "ping"}
            {"id": 2, "method": "call_tool", "tool": "click", "args": {...}, "agent_id": 42}
  Response: {"id": 1, "ok": true,  "result": "pong"}
            {"id": 2, "ok": true,  "result": {"content": [{"type": "text", "text": "..."}], "isError": false}}
            # call_tool result: an MCP CallToolResult dump — the tool's plain
            # dict rides as one JSON text block (the contract both the
            # per-agent wrapper and the direct dial validate against)
            {"id": 2, "ok": false, "error": "message"}

The per-agent bridge (`services/computer/mcp_wrapper.py`) and the MCP daemon's
direct dial (`ava/_mcp_computer.py`) speak this protocol; `agent_id` is stamped
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
    .venv/bin/python -m services.computer.mcp_daemon
"""

from __future__ import annotations

import asyncio
import json
import signal
from contextlib import suppress
from pathlib import Path
from typing import Any

# Re-export of the shared OCR module object (test compat: the suite patches
# mcp_daemon.ocr_mod attributes, and every OCR caller sees the same object).
from services.computer.execute import _TOOLS, _execute, _mcp_result, _priority
from services.computer.execute import ocr_mod as ocr_mod
from services.computer.protocol import Request, Response
from services.computer.session import ScreenSession
from services.computer.task_sessions import TaskSessionTracker
from services.permissions_helper import client as helper
from services.permissions_helper.client import PermissionsHelperError
from shared import audit_events
from shared.config import settings
from shared.log import logger
from shared.paths import computer_mcp_socket

# A snapshot PNG can be multi-MB on one line; lift the stream buffer cap well
# above StreamReader's 64KiB default (same limit as the browser daemon).
_LINE_LIMIT = 64 * 1024 * 1024


class ComputerMcpDaemon:
    """Unix-socket server for the computer-mcp line protocol."""

    def __init__(self, sock: str | None = None) -> None:
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
        # One lock around execute: a single desktop op at a time machine-wide,
        # and a snapshot's multi-step capture never interleaves with another
        # agent's click (same serial choice as browser-mcp).
        self._action_lock = asyncio.Lock()
        # Phase 2 session coordination: who owns the screen + FIFO waiters.
        self._screen = ScreenSession(
            lease_s=settings.daemon.computer_use_lease_s,
            queue_timeout_s=settings.daemon.computer_use_queue_timeout_s,
        )
        # Phase 2 audit envelope: task_id -> computer_session_start/end.
        self._task_sessions = TaskSessionTracker(idle_s=settings.daemon.computer_use_session_idle_s)
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
            with suppress(Exception):
                await writer.wait_closed()

    async def _dispatch(self, req: Request) -> Response:
        req_id = req.get("id")
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
                result = _execute(
                    tool,
                    args,
                    agent_id or 0,
                    pointer=self._pointer,
                    scale=self._scale,
                    ocr_cache=self._ocr_cache,
                )
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
            except (PermissionsHelperError, KeyError, TypeError, ValueError, OSError) as e:
                outcome, error = "error", f"{type(e).__name__}: {e}"
                result = None
            except Exception as e:  # unknown failure: audit + surface, stay alive
                outcome, error = "error", f"{type(e).__name__}: {e}"
                result = None
            if agent_id is not None:
                # A live caller renews the lease (success or failure — it is
                # still acting on the screen).
                await self._screen.touch(agent_id)
                task_id = args.get("task_id")
                if task_id is not None:
                    # Garbage task_id: the computer_action row still carries it
                    # as-is; the envelope just does not form (suppress, not a
                    # silent except:pass — the action is what matters here).
                    tid: int | None = None
                    with suppress(TypeError, ValueError):
                        tid = int(task_id)
                    if tid is not None:
                        try:
                            self._task_sessions.note(tid, agent_id, tool, self._emit_session_event)
                        except Exception as e:  # auxiliary path, see below
                            # The session envelope is auxiliary to the action
                            # itself: never fail the action, but never stay
                            # silent either — a contract mismatch (unregistered
                            # event name, FK hiccup) must be audible.
                            logger.warning(f"[computer-mcp] task-session event failed: {e}")
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

    @staticmethod
    def _emit_action(
        agent_id: int | None,
        tool: str,
        args: dict[str, Any],
        outcome: str,
        error: str | None,
        result: dict[str, Any] | None = None,
    ) -> None:
        """One computer_action audit event per call — facts for later review."""
        coords: str | None = None
        if tool == "click" and "x" in args:
            coords = f"{args['x']},{args['y']}"
        elif tool == "click_text" and result is not None:
            # click_text resolves its own target via OCR: audit the center it
            # clicked (physical pixels), not an argument coordinate.
            coords = f"{result.get('x')},{result.get('y')}"
        elif tool == "scroll":
            coords = f"{args.get('x')},{args.get('y')},{args.get('dy')}"
        elif tool == "key":
            coords = str(args.get("key") or args.get("keycode") or "")
        if agent_id is None:
            # No identity, no audit row: events.agent_id references agents(id).
            return
        app = None
        with suppress(Exception):
            app = helper.frontmost_app()["app"] or None
        audit_events.insert_event_log(
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

    @staticmethod
    def _emit_session_event(event_type: str, agent_id: int, payload: dict[str, Any]) -> None:
        """One computer_session_start/end audit row (no app lookup — the
        envelope describes the task, not a screen state)."""
        audit_events.insert_event_log(
            event_type=event_type,
            agent_id=agent_id,
            source=f"agent:{agent_id}",
            payload=payload,
        )


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
    with suppress(Exception):
        await writer.wait_closed()
    del reader  # nothing to close on a StreamReader; the writer close suffices
    return True


async def _serve_client(
    daemon: ComputerMcpDaemon, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """Run one client connection (the tracked task's coroutine)."""
    await daemon.handle(reader, writer)


def _tracked_client(
    daemon: ComputerMcpDaemon, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> asyncio.Task[None]:
    """Spawn the per-connection handler as a tracked task (cancellable at shutdown)."""
    task = asyncio.create_task(_serve_client(daemon, reader, writer))
    daemon._clients.add(task)
    task.add_done_callback(daemon._clients.discard)
    return task


async def run(sock: str | None = None) -> None:
    daemon = ComputerMcpDaemon(sock)
    path = Path(daemon._sock)
    if await _socket_in_use(path):
        logger.error(
            "[computer-mcp] socket %s is already served by a live daemon — "
            "refusing to start a second instance",
            path,
        )
        raise SystemExit(1)
    with suppress(OSError):
        path.unlink()
    server = await asyncio.start_unix_server(
        lambda r, w: _tracked_client(daemon, r, w), path=str(path), limit=_LINE_LIMIT
    )
    logger.info(f"[computer-mcp] listening on {path}")
    stop = asyncio.Event()

    def _stop() -> None:
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError):
            asyncio.get_running_loop().add_signal_handler(sig, _stop)
    try:
        await stop.wait()
    finally:
        server.close()
        # Close active clients first: server.wait_closed() waits for every
        # handler task, and a client holding its connection open (the SDK
        # keeps one persistent socket) would otherwise hang shutdown forever,
        # orphaning the process with the socket still bound.
        for task in list(daemon._clients):
            task.cancel()
        with suppress(Exception):
            await asyncio.gather(*daemon._clients, return_exceptions=True)
        await server.wait_closed()
        with suppress(OSError):
            path.unlink()
        logger.info("[computer-mcp] shutting down")


def main() -> None:
    from shared.log import init_gateway_process

    init_gateway_process(name="computer-mcp")
    asyncio.run(run())


if __name__ == "__main__":
    main()
