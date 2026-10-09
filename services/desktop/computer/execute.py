"""MCP tool declarations and per-tool execution for the computer-mcp daemon."""

from __future__ import annotations

import json
from typing import Any

from ..permissions_helper import client as helper
from . import ax_act, ax_tools
from . import ocr as ocr_mod
from .ax_ids import AxSession
from .errors import ComputerUseError
from .input import (
    ClickInput,
    DragInput,
    KeyInput,
    MoveInput,
    ScrollInput,
    tool_schema,
    validate_input,
)
from .ocr_text import _click_text_tool, _find_text_tool
from .screen import (
    _capture_screen,
    _current_scale,
    _png_size,
    _snapshot_path,
    _to_logical,
    capture_region,
)
from .targets import (
    AppTarget,
    CaptureFrame,
    CaptureRegion,
    CoordinateSpace,
    ObservationFrame,
    WindowTarget,
    app_selector,
)

# Required arguments per tool. The MCP input schemas declare them; the daemon
# enforces them too, so a missing argument fails with a readable message
# instead of a bare KeyError leaking out of the helper call.
_REQUIRED_ARGS: dict[str, tuple[str, ...]] = {
    "click": ("x", "y"),
    "drag": ("start_x", "start_y", "end_x", "end_y"),
    "type_text": ("text",),
    "move": ("x", "y"),
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
    "cmd": 55,
    "shift": 56,
    "alt": 58,
    "ctrl": 59,
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


def _snapshot_tool(
    args: dict[str, Any], agent_id: int, ocr_cache: dict[str, Any] | None
) -> dict[str, Any]:
    if "region" in args and "target" in args:
        raise ComputerUseError("snapshot accepts region or target, not both")
    if "region" in args or "target" in args:
        if args.get("include_ax"):
            raise ComputerUseError("include_ax is available only for a whole-screen snapshot")
        if "region" in args:
            region = CaptureRegion.parse(args["region"])
            path, scale, (pw, ph) = capture_region(agent_id, region)
            frame = CaptureFrame(CoordinateSpace.REGION_PIXELS, region.x, region.y, scale, pw, ph)
            source = "region"
        else:
            target = WindowTarget.parse(args["target"])
            path = _snapshot_path(agent_id)
            captured = helper.screencapture_window(target.pid, target.window_id, str(path))
            pw, ph = _png_size(path)
            if (pw, ph) != (captured["width"], captured["height"]):
                raise ComputerUseError("window image dimensions do not match the helper response")
            frame = CaptureFrame.parse(
                {
                    "coordinate_space": CoordinateSpace.WINDOW_PIXELS.value,
                    "origin": captured["origin"],
                    "scale": captured["scale"],
                    "pixels": {"width": pw, "height": ph},
                    "target": {"pid": target.pid, "window_id": target.window_id},
                }
            )
            source = "window"
        result: dict[str, Any] = {
            "path": str(path),
            "source": source,
            "frame": frame.as_dict(),
            "pixels": {"width": pw, "height": ph},
        }
    else:
        path, size, scale, (pw, ph) = _capture_screen(agent_id)
        result = {
            "path": str(path),
            "source": "screen",
            "screen": {"width": size["w"], "height": size["h"], "scale": scale},
            "pixels": {"width": pw, "height": ph},
        }
    if args.get("include_ax"):
        scale = float(result["screen"]["scale"])
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
            if ocr_cache is not None and "frame" not in result:
                ocr_cache["items"] = result["ocr"]
        except ocr_mod.OcrError as e:
            result["ocr"] = []
            result["ocr_error"] = str(e)
    return result


def _logical_point(
    x: float, y: float, frame: ObservationFrame | None, scale: float | None
) -> tuple[float, float]:
    if frame is not None:
        return CaptureFrame.parse(frame).global_point(x, y)
    scale = _current_scale(scale)
    return _to_logical(x, scale), _to_logical(y, scale)


def _click_tool(args: dict[str, Any], scale: float | None) -> dict[str, Any]:
    request = validate_input(ClickInput, args)
    lx, ly = _logical_point(request.x, request.y, request.frame, scale)
    options = request.model_dump(exclude_unset=True, exclude={"x", "y", "frame"})
    options.setdefault("double", False)
    return dict(helper.click(lx, ly, **options))


def _key_tool(args: dict[str, Any]) -> dict[str, Any]:
    request = validate_input(KeyInput, args)
    code = request.keycode if request.keycode is not None else _keycode_for(request.key or "")
    if code is None:
        raise ComputerUseError("unknown key name; use a supported name or integer keycode")
    options = request.model_dump(exclude_unset=True, exclude={"key", "keycode"})
    options.setdefault("cmd", False)
    echoed = helper.key(code, **options)
    return {"pressed": echoed["key"], "cmd": echoed["cmd"]}


def _drag_tool(args: dict[str, Any], scale: float | None) -> dict[str, Any]:
    request = validate_input(DragInput, args)
    start = _logical_point(request.start_x, request.start_y, request.frame, scale)
    end = _logical_point(request.end_x, request.end_y, request.frame, scale)
    return dict(helper.drag(*start, *end))


def _move_tool(args: dict[str, Any], scale: float | None) -> dict[str, Any]:
    request = validate_input(MoveInput, args)
    lx, ly = _logical_point(request.x, request.y, request.frame, scale)
    return dict(helper.move(lx, ly, modifiers=list(request.modifiers)))


def _scroll_tool(args: dict[str, Any], scale: float | None) -> dict[str, Any]:
    request = validate_input(ScrollInput, args)
    if request.x is not None and request.y is not None:
        lx, ly = _logical_point(request.x, request.y, request.frame, scale)
    else:
        cursor = helper.cursor_position()
        lx, ly = cursor["x"], cursor["y"]
    options = request.model_dump(exclude_unset=True, exclude={"x", "y", "dy", "frame"})
    return dict(helper.scroll(lx, ly, request.dy, **options))


def _window_info_tool(args: dict[str, Any]) -> dict[str, Any]:
    # owner is optional — the caller usually wants the focused window, and
    # defaulting here saves a frontmost_app round trip.
    owner = args.get("owner") or helper.frontmost_app()["app"]
    if not owner:
        raise ComputerUseError("window_info needs an owner and no app is frontmost")
    return {**helper.window_info(str(owner))}


def _application_tool(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    if tool == "list_apps":
        return dict(helper.list_apps())
    if tool == "list_windows":
        return dict(helper.list_windows(app_selector(args.get("app"))))
    if tool == "focus_app":
        target = AppTarget.parse(args.get("target"))
        selector: dict[str, object] = (
            {"pid": target.pid} if target.pid is not None else {"bundle_id": target.bundle_id}
        )
        return dict(helper.focus_app(selector))
    if tool == "window_info":
        return _window_info_tool(args)
    if tool == "session_info":
        return {**helper.session_info()}
    if tool == "frontmost_app":
        return {**helper.frontmost_app()}
    raise ComputerUseError(f"unknown application tool {tool!r}")


def _execute(
    tool: str,
    args: dict[str, Any],
    agent_id: int,
    pointer: tuple[float, float] | None = None,
    scale: float | None = None,
    ocr_cache: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one tool against the permissions helper. Raises on failure.

    `pointer` is retained for callers of the existing internal interface;
    scroll reads the live native cursor without explicit x/y. `scale` is the
    last measured physical->logical scale, falling back to the helper report.
    `ocr_cache` carries the last OCR text boxes (snapshot include_ocr /
    find_text / click_text), reused by find_text(snapshot_fresh=false)."""
    del pointer  # Retain the internal call signature; cursor reads are now live.
    _require(tool, args)
    if tool == "snapshot":
        return _snapshot_tool(args, agent_id, ocr_cache)
    if tool == "find_text":
        return _find_text_tool(args, agent_id, ocr_cache)
    if tool == "click":
        return _click_tool(args, scale)
    if tool == "drag":
        return _drag_tool(args, scale)
    if tool == "click_text":
        return _click_text_tool(args, agent_id, ocr_cache)
    if tool == "type_text":
        return {"typed": helper.type_text(str(args["text"]))["typed"]}
    if tool == "key":
        return _key_tool(args)
    if tool == "scroll":
        return _scroll_tool(args, scale)
    if tool == "move":
        return _move_tool(args, scale)
    if tool == "cursor_position":
        position = helper.cursor_position()
        current = _current_scale(scale)
        return {"x": position["x"] * current, "y": position["y"] * current, "scale": current}
    if tool in {
        "list_apps",
        "list_windows",
        "focus_app",
        "window_info",
        "session_info",
        "frontmost_app",
    }:
        return _application_tool(tool, args)
    raise ComputerUseError(f"unknown tool {tool!r}")


_AX_TOOLS = {"ax_tree": ax_tools.ax_tree_tool, "ax_act": ax_act.ax_act_tool}


def _execute_tool(
    tool: str,
    args: dict[str, Any],
    agent_id: int,
    pointer: tuple[float, float] | None = None,
    scale: float | None = None,
    ocr_cache: dict[str, Any] | None = None,
    ax_session: AxSession | None = None,
) -> dict[str, Any]:
    """The daemon's entry: the accessibility tools, else `_execute`."""
    if tool in _AX_TOOLS:
        return _AX_TOOLS[tool](args, agent_id, scale, ax_session or AxSession())
    return _execute(tool, args, agent_id, pointer=pointer, scale=scale, ocr_cache=ocr_cache)


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
            "Capture the full screen, a logical region, or a PID/window_id target via the helper. "
            "Region/window captures return an explicit frame; pass a region frame to pointer tools. "
            "Window frame input is unsupported; explicitly focus_app and capture again. Returns the PNG "
            "path (physical pixels), the logical screen size, and the measured backing "
            "scale, divide physical pixel coordinates by scale for click coordinates. "
            "include_ax adds the focused window's geometry in physical pixels (same "
            "space as click); include_ocr adds recognized text with physical-pixel "
            "boxes — OCR failure degrades to ocr:[] + ocr_error, never failing it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "region": {
                    "type": "object",
                    "properties": {
                        "x": {"type": "integer"},
                        "y": {"type": "integer"},
                        "w": {"type": "integer", "minimum": 1},
                        "h": {"type": "integer", "minimum": 1},
                    },
                    "required": ["x", "y", "w", "h"],
                    "additionalProperties": False,
                },
                "target": {
                    "type": "object",
                    "properties": {
                        "pid": {"type": "integer", "minimum": 1},
                        "window_id": {"type": "integer", "minimum": 1},
                    },
                    "required": ["pid", "window_id"],
                    "additionalProperties": False,
                },
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
            "Click a mouse button at physical-pixel screen coordinates "
            "(the daemon converts with the measured scale, falling back to the "
            "helper's live report). button selects left/right/middle; click_count selects one to three presses. modifiers holds shift/ctrl/alt/cmd flags throughout; duration_ms holds each press up to 5000 ms. double=True remains supported. Pass a region snapshot frame to use screenshot-local pixel coordinates."
        ),
        "input_schema": tool_schema(ClickInput),
    },
    {
        "name": "drag",
        "description": (
            "Drag the left mouse button from start_x/start_y to end_x/end_y in "
            "physical-pixel screen coordinates (the same space as snapshot and click). "
            "The daemon converts both endpoints with the measured scale. "
            "Performs one short straight-line drag and releases the button."
        ),
        "input_schema": tool_schema(DragInput),
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
            "Press one key with optional modifiers and duration_ms hold (0..10000 ms). Pass `key` as a name "
            "('return', 'escape', 'tab', 'space', 'up', 'down', 'left', 'right', "
            "'home', 'end', 'pageup', 'pagedown', 'backspace', 'delete', "
            "'F1'-'F12') or a single character ('a'-'z', '0'-'9'); or pass "
            "`keycode` for a raw macOS virtual keycode."
        ),
        "input_schema": tool_schema(KeyInput),
    },
    {
        "name": "scroll",
        "description": (
            "Scroll by dx/dy pixels horizontally/vertically with optional modifiers. "
            "Optional x/y position the pointer first (physical pixels, or local pixels "
            "with a region frame); otherwise use the actual current cursor position."
        ),
        "input_schema": tool_schema(ScrollInput),
    },
    {
        "name": "move",
        "description": "Move the pointer without clicking; physical pixels or local region frame pixels.",
        "input_schema": tool_schema(MoveInput),
    },
    {
        "name": "cursor_position",
        "description": "Read the actual pointer position in physical screen pixels using the current scale.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_apps",
        "description": "List running application PID, display name and bundle ID; nullable metadata stays null.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_windows",
        "description": "List window IDs, owning PIDs and logical geometry, including off-screen windows. Optional exact app name/bundle ID must be unique. Requires Screen Recording.",
        "input_schema": {
            "type": "object",
            "properties": {"app": {"type": "string", "minLength": 1}},
        },
    },
    {
        "name": "focus_app",
        "description": "Explicitly activate a running app selected by PID or unique bundle ID and confirm focused PID. Capture again afterwards; this changes the foreground app.",
        "input_schema": {
            "type": "object",
            "properties": {
                "target": {
                    "oneOf": [
                        {
                            "type": "object",
                            "properties": {"pid": {"type": "integer", "minimum": 1}},
                            "required": ["pid"],
                            "additionalProperties": False,
                        },
                        {
                            "type": "object",
                            "properties": {"bundle_id": {"type": "string", "minLength": 1}},
                            "required": ["bundle_id"],
                            "additionalProperties": False,
                        },
                    ]
                }
            },
            "required": ["target"],
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
    *ax_tools.TOOL_DECLARATIONS,
    *ax_act.TOOL_DECLARATIONS,
]
