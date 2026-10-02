"""Element actions for the computer-mcp daemon: `ax_act`.

Acts on an element id (`eN`) that `ax_tree` returned: press, show_menu, focus
or set_value, through the accessibility API — no pointer movement, and the
target app need not be frontmost. The helper acts by the raw id of its latest
walk and refuses (stale) when that element is gone or changed; this module
turns that into one transparent retry: re-walk the same app, re-find the
element by its fingerprint (`ax_ids`), and act again. When the element is
still not there, the caller gets a readable error and re-reads the tree — a
wrong element is never acted on.

`set_value` carries text the caller may consider private: it goes to the helper
and into the target field, and is not echoed in the result, the error text or
the audit event.
"""

from __future__ import annotations

import re
from typing import Any

from services.computer.ax_ids import AxIdTable, AxSession
from services.computer.ax_tools import require_helper_support
from services.computer.errors import ComputerUseError
from services.computer.screen import _current_scale
from services.permissions_helper import client as helper
from services.permissions_helper.client import AxActResult

ACTIONS: tuple[str, ...] = ("press", "set_value", "focus", "show_menu")
_MAX_VALUE_CHARS = 10_000
_ID_RE = re.compile(r"^e(\d+)$")
_UNANSWERED_NOTE = (
    "the app did not answer in time; the action may still have run — re-read with ax_tree"
)


def _args(args: dict[str, Any]) -> tuple[int, str, str | None]:
    match = _ID_RE.match(str(args.get("id", "")))
    if match is None:
        raise ComputerUseError(f"id must be an element id like 'e12', got {args.get('id')!r}")
    action = str(args.get("action", ""))
    if action not in ACTIONS:
        raise ComputerUseError(f"action must be one of {', '.join(ACTIONS)}, got {action!r}")
    value = args.get("value")
    if action == "set_value":
        if not isinstance(value, str):
            raise ComputerUseError("set_value requires a string value")
        if len(value) > _MAX_VALUE_CHARS:
            raise ComputerUseError(f"value is longer than {_MAX_VALUE_CHARS} characters")
    elif value is not None:
        raise ComputerUseError(f"{action} takes no value")
    return int(match.group(1)), action, value


_VISUAL_ID_RE = re.compile(r"^px:(\d+)$")


def _press_visual(args: dict[str, Any], session: AxSession) -> dict[str, Any]:
    """`px:N` (a visual-only text from `ax_tree(include_ocr_gap)`): the only
    thing that can be done with it is a click at its center."""
    match = _VISUAL_ID_RE.match(str(args["id"]))
    if match is None:
        raise ComputerUseError(f"id must look like 'px:3', got {args['id']!r}")
    if args.get("action") != "press" or args.get("value") is not None:
        raise ComputerUseError("px: elements only support action=press (a click at the text)")
    number = int(match.group(1))
    if number not in session.visual:
        raise ComputerUseError(
            f"unknown visual element px:{number}: call ax_tree with include_ocr_gap first"
        )
    x, y, text = session.visual[number]
    helper.click(x / session.visual_scale, y / session.visual_scale)
    return {
        "element": f"px:{number}",
        "action": "press",
        "completed": True,
        "label": text[:80],
        "x": x,
        "y": y,
    }


def _attempt(table: AxIdTable, sid: int, action: str, value: str | None) -> AxActResult | None:
    """One try through the element's current raw id; None when it has none."""
    raw = table.entry(sid).raw
    if raw is None:
        return None
    result = helper.ax_act(table.app, raw, action, value=value)
    return None if result.get("stale") else result


def _rewalk(table: AxIdTable) -> None:
    """Re-read the app's window so fingerprints can re-find the element. A
    restarted app (new pid) invalidates every id."""
    walked = helper.ax_tree(table.app)
    if walked["pid"] != table.pid:
        raise ComputerUseError(f"{table.app} was restarted: call ax_tree again")
    table.align(walked["nodes"], scoped=False)


def ax_act_tool(
    args: dict[str, Any], _agent_id: int, scale: float | None, session: AxSession
) -> dict[str, Any]:
    """`ax_act`: press / show_menu / focus / set_value on an `ax_tree` element."""
    if str(args.get("id", "")).startswith("px:"):
        return _press_visual(args, session)
    sid, action, value = _args(args)
    table = session.current()
    table.entry(sid)  # unknown ids fail before any helper round trip
    require_helper_support("ax_act_v1", "ax_act")
    result = _attempt(table, sid, action, value)
    if result is None:
        _rewalk(table)
        result = _attempt(table, sid, action, value)
    if result is None:
        raise ComputerUseError(
            f"element e{sid} is gone or changed in {table.app}: call ax_tree again"
        )
    out: dict[str, Any] = {
        "element": f"e{sid}",
        "action": action,
        "completed": result["completed"],
    }
    if "role" in result:
        out["role"] = result["role"]
    if "label" in result:
        out["label"] = result["label"]
    if "x" in result and "y" in result and "w" in result and "h" in result:
        factor = _current_scale(scale)
        out["x"] = round((result["x"] + result["w"] / 2) * factor)
        out["y"] = round((result["y"] + result["h"] / 2) * factor)
    if result.get("unanswered"):
        out["note"] = _UNANSWERED_NOTE
    return out


TOOL_DECLARATIONS: list[dict[str, Any]] = [
    {
        "name": "ax_act",
        "description": (
            "Act on an element id (eN) from ax_tree through the accessibility API — no "
            "mouse movement, and the app need not be in front. action: press (buttons, "
            "checkboxes, menu items, tabs, links), set_value (replace the text of an "
            "editable field; needs value, which is never echoed or logged), focus, "
            "show_menu. If the tree changed since ax_tree it re-finds the element by "
            "its place in the tree and acts once; if it is gone or changed you get an "
            "error — call ax_tree again, never guess an id. A px:N id (visual-only text from "
            "ax_tree include_ocr_gap) accepts only action=press, a click at its center. Returns the element's "
            "center (x, y in physical pixels) and completed; completed=false with a "
            "note means the app did not answer in time and the action may still have "
            "run, so re-read the tree. A UI change after an action can renumber nearby "
            "elements: read ax_tree again before the next one. When an element has no "
            "usable action, use click at its center instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "action": {"type": "string", "enum": list(ACTIONS)},
                "value": {"type": "string"},
                "task_id": {"type": "integer"},
                "priority": {"type": "string", "enum": ["normal", "high"], "default": "normal"},
            },
            "required": ["id", "action"],
        },
    },
]
