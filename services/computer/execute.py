"""MCP tool declarations and per-tool execution for the computer-mcp daemon."""

from __future__ import annotations

import json
from typing import Any

import services.computer.ocr as ocr_mod
from services.computer.errors import ComputerUseError
from services.computer.ocr_text import _click_text_tool, _find_text_tool
from services.computer.screen import _capture_screen, _current_scale, _to_logical
from services.permissions_helper import client as helper

# Required arguments per tool. The MCP input schemas declare them; the daemon
# enforces them too, so a missing argument fails with a readable message
# instead of a bare KeyError leaking out of the helper call.
_REQUIRED_ARGS: dict[str, tuple[str, ...]] = {
    "click": ("x", "y"),
    "type_text": ("text",),
    "scroll": ("dy",),
    "find_text": ("text",),
    "click_text": ("text",),
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
    ocr_cache: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one tool against the permissions helper. Raises on failure.

    `pointer` is the tracked cursor position in PHYSICAL pixels (last
    click/scroll), the scroll fallback without explicit x/y; `scale` is the
    last measured physical->logical scale, falling back to the helper report.
    `ocr_cache` carries the last OCR text boxes (snapshot include_ocr /
    find_text / click_text), reused by find_text(snapshot_fresh=false)."""
    _require(tool, args)
    if tool == "snapshot":
        path, size, scale, (pw, ph) = _capture_screen(agent_id)
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
                if ocr_cache is not None:
                    ocr_cache["items"] = result["ocr"]
            except ocr_mod.OcrError as e:
                result["ocr"] = []
                result["ocr_error"] = str(e)
        return result
    if tool == "find_text":
        return _find_text_tool(args, agent_id, ocr_cache)
    if tool == "click":
        scale = _current_scale(scale)
        clicked = helper.click(
            _to_logical(float(args["x"]), scale),
            _to_logical(float(args["y"]), scale),
            double=bool(args.get("double", False)),
        )
        return {"clicked": clicked["clicked"], "double": clicked["double"]}
    if tool == "click_text":
        return _click_text_tool(args, agent_id, ocr_cache)
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
        "name": "find_text",
        "description": (
            "OCR the screen and find text, returning every matching box with "
            "physical-pixel geometry (x/y/w/h + center cx/cy — the click space). "
            "match=contains (substring) or exact, both case-insensitive; matches "
            "come top-to-bottom then left-to-right, each carrying its index for "
            "click_text. Fresh capture by default; snapshot_fresh=false searches "
            "the last OCR result instead (from a snapshot include_ocr or a prior "
            "find_text — result says fresh:false when reused). Errors (never an "
            "empty list) when OCR itself fails."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "match": {"type": "string", "enum": ["contains", "exact"], "default": "contains"},
                "snapshot_fresh": {"type": "boolean", "default": True},
                "task_id": {"type": "integer"},
                "priority": {"type": "string", "enum": ["normal", "high"], "default": "normal"},
            },
            "required": ["text"],
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
        "name": "click_text",
        "description": (
            "OCR the screen and click the center of the text box matching "
            "`text` (match=contains or exact, case-insensitive) — one action "
            "for the OCR -> locate -> click path. index picks among multiple "
            "matches in the same top-to-bottom, left-to-right order find_text "
            "returns. Always reads the screen fresh right before the click. "
            "Fails with a readable error when nothing matches or index is out "
            "of range — never clicks blind."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "match": {"type": "string", "enum": ["contains", "exact"], "default": "contains"},
                "index": {"type": "integer", "default": 0},
                "task_id": {"type": "integer"},
                "priority": {"type": "string", "enum": ["normal", "high"], "default": "normal"},
            },
            "required": ["text"],
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
