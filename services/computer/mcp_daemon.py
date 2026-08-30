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
import struct
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from services.computer import ocr as ocr_mod
from services.computer.protocol import Request, Response
from services.computer.session import ScreenSession
from services.computer.task_sessions import TaskSessionTracker
from services.permissions_helper import client as helper
from services.permissions_helper.client import PermissionsHelperError
from shared import audit_events
from shared.config import settings
from shared.log import logger
from shared.paths import computer_mcp_socket, logs_dir

# A snapshot PNG can be multi-MB on one line; lift the stream buffer cap well
# above StreamReader's 64KiB default (same limit as the browser daemon).
_LINE_LIMIT = 64 * 1024 * 1024


class ComputerUseError(Exception):
    """A tool call failed with a readable, operator/agent-facing message."""


def _png_size(path: Path) -> tuple[int, int]:
    """Physical pixel size of a PNG from its IHDR (signature + 4-byte length +
    'IHDR' + width + height, big-endian). No image library needed."""
    with Path(path).open("rb") as f:
        head = f.read(24)
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        raise ComputerUseError(f"captured file {path} is not a PNG")
    width, height = struct.unpack(">II", head[16:24])
    return width, height


def _to_logical(value: float, scale: float) -> float:
    """Physical-pixel coordinate -> helper's logical-point space (divide by the
    measured backing scale; scale 1 (1x display) is a no-op there)."""
    return value / scale if scale > 1 else value


def _pixel_scale(pixels_w: int, logical_w: float) -> float:
    """Measure the physical->logical scale from the captured PNG itself.

    The PNG is the ground truth of the click space (IHDR width = physical
    pixels of exactly the region the caller clicks); the helper's report is
    not trusted here — a process without an AppKit event loop can hold stale
    screen objects (2026-08-30: scale 2 on a 1x display, halving every click).
    """
    if pixels_w <= 0 or logical_w <= 0:
        raise ComputerUseError(f"cannot compute screen scale from {pixels_w}px over {logical_w}pt")
    return pixels_w / logical_w


def _current_scale(measured: float | None) -> float:
    """The physical->logical scale for coordinate conversion.

    Prefers the daemon's own measurement (last snapshot, ``_pixel_scale``);
    before any snapshot, falls back to the helper's live report.
    """
    if measured is not None:
        return measured
    return float(helper.screen_size()["scale"])


def _snapshot_path(agent_id: int | None) -> Path:
    """A fresh capture path under $AVA_HOME/logs/computer/snapshots/."""
    directory = logs_dir() / "computer" / "snapshots"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    return directory / f"agent-{agent_id or 0}-{stamp}.png"


# Required arguments per tool. The MCP input schemas declare them; the daemon
# enforces them too, so a missing argument fails with a readable message
# instead of a bare KeyError leaking out of the helper call.
_REQUIRED_ARGS: dict[str, tuple[str, ...]] = {
    "click": ("x", "y"),
    "type_text": ("text",),
    "scroll": ("dy",),
}


def _priority(args: dict[str, Any]) -> str:
    """The caller's queue priority — only "high" is special, anything else
    (absent, garbage) is normal. Resource coordination, not governance: this
    shifts order in the queue, it never grants or denies."""
    return "high" if args.get("priority") == "high" else "normal"


def _require(tool: str, args: dict[str, Any]) -> None:
    """Fail fast with a readable message when a required argument is absent."""
    for key in _REQUIRED_ARGS.get(tool, ()):
        if key not in args:
            raise ComputerUseError(f"{tool} requires argument {key!r}")


# macOS virtual keycodes for the key tool's name/character vocabulary. LLM
# callers cannot be expected to know raw keycodes, so the MCP surface takes
# names ('return', 'space', ...) or single characters; the raw keycode stays
# available via the optional `keycode` argument (the helper API is code-based).
_KEYCODES: dict[str, int] = {
    "a": 0,
    "s": 1,
    "d": 2,
    "f": 3,
    "h": 4,
    "g": 5,
    "z": 6,
    "x": 7,
    "c": 8,
    "v": 9,
    "b": 11,
    "q": 12,
    "w": 13,
    "e": 14,
    "r": 15,
    "y": 16,
    "t": 17,
    "1": 18,
    "2": 19,
    "3": 20,
    "4": 21,
    "6": 22,
    "5": 23,
    "=": 24,
    "9": 25,
    "7": 26,
    "-": 27,
    "8": 28,
    "0": 29,
    "]": 30,
    "o": 31,
    "u": 32,
    "[": 33,
    "i": 34,
    "p": 35,
    "l": 37,
    "j": 38,
    "'": 39,
    "k": 40,
    ";": 41,
    "\\": 42,
    ",": 43,
    "/": 44,
    "n": 45,
    "m": 46,
    ".": 47,
    "`": 50,
    "return": 36,
    "enter": 36,
    "tab": 48,
    "space": 49,
    "backspace": 51,
    "delete": 51,
    "escape": 53,
    "esc": 53,
    "home": 115,
    "end": 119,
    "pageup": 116,
    "pagedown": 121,
    "forwarddelete": 117,
    "left": 123,
    "right": 124,
    "down": 125,
    "up": 126,
    "f1": 122,
    "f2": 120,
    "f3": 99,
    "f4": 118,
    "f5": 96,
    "f6": 97,
    "f7": 98,
    "f8": 100,
    "f9": 101,
    "f10": 109,
    "f11": 103,
    "f12": 111,
}


def _keycode_for(key: str) -> int | None:
    """Virtual keycode for a key name or single character (case-insensitive)."""
    return _KEYCODES.get(key.lower())


def _mcp_result(result: dict[str, Any]) -> dict[str, Any]:
    """Wrap a tool's plain-dict result in the MCP CallToolResult shape.

    The per-agent wrapper and the direct dial both validate the daemon's
    call_tool result as `mcp.types.CallToolResult`, which requires a `content`
    block list; the semantic dict rides as one JSON text block."""
    return {
        "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
        "isError": False,
    }


def _execute(
    tool: str,
    args: dict[str, Any],
    agent_id: int,
    pointer: tuple[float, float] | None = None,
    scale: float | None = None,
) -> dict[str, Any]:
    """Run one tool against the permissions helper. Raises on failure.

    `pointer` is the tracked cursor position in PHYSICAL pixels (last
    click/scroll), the scroll fallback without explicit x/y; `scale` is the
    last measured physical->logical scale, falling back to the helper report."""
    _require(tool, args)
    if tool == "snapshot":
        path = _snapshot_path(agent_id)
        size = helper.screen_size()
        # screencapture -R takes logical points and clips to the display; the
        # PNG comes out at physical resolution (Retina 2x), reported via IHDR.
        helper.screencapture_region(0, 0, int(size["w"]), int(size["h"]), str(path))
        pw, ph = _png_size(path)
        # Measure, don't trust: the PNG is the ground truth callers click
        # against; the helper's reported scale can be stale (see _pixel_scale).
        scale = _pixel_scale(pw, size["w"])
        result: dict[str, Any] = {
            "path": str(path),
            "screen": {"width": size["w"], "height": size["h"], "scale": scale},
            "pixels": {"width": pw, "height": ph},
        }
        if args.get("include_ax"):
            app = helper.frontmost_app()["app"]
            if app:
                ax = helper.ax_window_info(app)
                # AX geometry is logical; convert to the click space like OCR.
                result["ax"] = {
                    **ax,
                    "x": ax["x"] * scale,
                    "y": ax["y"] * scale,
                    "w": ax["w"] * scale,
                    "h": ax["h"] * scale,
                }
        if args.get("include_ocr"):
            # Soft failure: snapshot stays usable without text recognition.
            try:
                result["ocr"] = ocr_mod.ocr_image(path)
            except ocr_mod.OcrError as e:
                result["ocr"] = []
                result["ocr_error"] = str(e)
        return result
    if tool == "click":
        scale = _current_scale(scale)
        clicked = helper.click(
            _to_logical(float(args["x"]), scale),
            _to_logical(float(args["y"]), scale),
            double=bool(args.get("double", False)),
        )
        return {"clicked": clicked["clicked"], "double": clicked["double"]}
    if tool == "type_text":
        return {"typed": helper.type_text(str(args["text"]))["typed"]}
    if tool == "key":
        if "keycode" in args:
            code = int(args["keycode"])
        else:
            code = _keycode_for(str(args.get("key") or ""))
        if code is None:
            raise ComputerUseError(
                "key needs a key name ('return', 'space', 'a', ...) or an integer keycode"
            )
        # The helper echoes {"key": code, "cmd": ...}; the MCP contract keeps
        # the "pressed" name the callers read, so map the echo through.
        echoed = helper.key(code, cmd=bool(args.get("cmd", False)))
        return {"pressed": echoed["key"], "cmd": echoed["cmd"]}
    if tool == "scroll":
        dy = int(args["dy"])
        scale = _current_scale(scale)
        if "x" in args and "y" in args:
            lx, ly = _to_logical(float(args["x"]), scale), _to_logical(float(args["y"]), scale)
        elif pointer is not None:
            lx, ly = _to_logical(pointer[0], scale), _to_logical(pointer[1], scale)
        else:
            size = helper.screen_size()
            # Center from the helper is already logical — never re-divide
            # (that double conversion scrolled at a quarter of the screen).
            lx, ly = size["w"] / 2, size["h"] / 2
        return {"scrolled": helper.scroll(lx, ly, dy)["scrolled"]}
    if tool == "window_info":
        # owner is optional — the caller usually wants the focused window, and
        # defaulting here saves a frontmost_app round trip.
        owner = args.get("owner") or helper.frontmost_app()["app"]
        if not owner:
            raise ComputerUseError("window_info needs an owner and no app is frontmost")
        return {**helper.window_info(str(owner))}
    if tool == "session_info":
        return {**helper.session_info()}
    if tool == "frontmost_app":
        return {**helper.frontmost_app()}
    raise ComputerUseError(f"unknown tool {tool!r}")


# MCP tool declarations (list_tools shape: name / description / input_schema).
_TOOLS: list[dict[str, Any]] = [
    {
        "name": "release_control",
        "description": (
            "Release the screen so the next FIFO waiter can act — call it when a "
            "multi-step desktop flow is done (peers queue behind a held screen). "
            "No-op with an error when you are not the current holder; the holder's "
            "lease also expires on its own after the lease timeout without actions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "priority": {"type": "string", "enum": ["normal", "high"], "default": "normal"},
            },
        },
    },
    {
        "name": "snapshot",
        "description": (
            "Capture the full screen via the signed permissions helper. Returns the PNG "
            "path (physical pixels), the logical screen size, and the measured backing "
            "scale, divide physical pixel coordinates by scale for click coordinates. "
            "include_ax adds the focused window's geometry in physical pixels (same "
            "space as click); include_ocr adds recognized text with physical-pixel "
            "boxes — OCR failure degrades to ocr:[] + ocr_error, never failing it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "include_ax": {"type": "boolean", "default": False},
                "include_ocr": {"type": "boolean", "default": False},
                "task_id": {"type": "integer"},
                "priority": {"type": "string", "enum": ["normal", "high"], "default": "normal"},
            },
        },
    },
    {
        "name": "click",
        "description": (
            "Click the left mouse button at physical-pixel screen coordinates "
            "(the daemon converts with the measured scale, falling back to the "
            "helper's live report). double=True double-clicks."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "x": {"type": "number"},
                "y": {"type": "number"},
                "double": {"type": "boolean", "default": False},
                "task_id": {"type": "integer"},
                "priority": {"type": "string", "enum": ["normal", "high"], "default": "normal"},
            },
            "required": ["x", "y"],
        },
    },
    {
        "name": "type_text",
        "description": "Type a UTF-8 string into the focused field (handles CJK).",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "task_id": {"type": "integer"},
                "priority": {"type": "string", "enum": ["normal", "high"], "default": "normal"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "key",
        "description": (
            "Press one key, optionally with Command held. Pass `key` as a name "
            "('return', 'escape', 'tab', 'space', 'up', 'down', 'left', 'right', "
            "'home', 'end', 'pageup', 'pagedown', 'backspace', 'delete', "
            "'F1'-'F12') or a single character ('a'-'z', '0'-'9'); or pass "
            "`keycode` for a raw macOS virtual keycode."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "keycode": {"type": "integer"},
                "cmd": {"type": "boolean", "default": False},
                "task_id": {"type": "integer"},
                "priority": {"type": "string", "enum": ["normal", "high"], "default": "normal"},
            },
        },
    },
    {
        "name": "scroll",
        "description": (
            "Scroll vertically by dy pixels (negative = toward older content). "
            "Optional x/y move the pointer there first (physical pixels); "
            "without them the scroll happens at the current pointer position "
            "(the last click/scroll), or the screen center before any pointer "
            "move."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "x": {"type": "number"},
                "y": {"type": "number"},
                "dy": {"type": "integer"},
                "task_id": {"type": "integer"},
                "priority": {"type": "string", "enum": ["normal", "high"], "default": "normal"},
            },
            "required": ["dy"],
        },
    },
    {
        "name": "window_info",
        "description": (
            "Geometry of an app's normal on-screen window. Omit owner to use the frontmost app."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "task_id": {"type": "integer"},
                "priority": {"type": "string", "enum": ["normal", "high"], "default": "normal"},
            },
        },
    },
    {
        "name": "session_info",
        "description": "Whether the login session is locked or off-console.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "priority": {"type": "string", "enum": ["normal", "high"], "default": "normal"},
            },
        },
    },
    {
        "name": "frontmost_app",
        "description": "Display name of the frontmost application ( when none).",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "priority": {"type": "string", "enum": ["normal", "high"], "default": "normal"},
            },
        },
    },
]


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
                    tool, args, agent_id or 0, pointer=self._pointer, scale=self._scale
                )
                if tool == "snapshot":
                    # click/scroll convert with the scale the caller saw.
                    self._scale = float(result["screen"]["scale"])
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
        lambda r, w: _tracked_client(daemon, r, w), path=str(path)
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
