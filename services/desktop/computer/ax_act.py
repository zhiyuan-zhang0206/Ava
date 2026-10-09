"""Element actions for the computer-mcp daemon: `ax_act`.

Acts on an element id (`eN`) that `ax_tree` returned: press, show_menu, focus
or set_value, execute a reported action, or select text through the accessibility
API. No pointer movement; the target app need not be frontmost. The helper acts by the raw id of its latest
walk and refuses stale elements or windows. A stale result requires a fresh
caller observation; it never re-walks another focused window automatically.

`set_value` carries text the caller may consider private: it goes to the helper
and into the target field, and is not echoed in the result, the error text or
the audit event.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from services.desktop.computer.ax_ids import AxIdTable, AxSession
from services.desktop.computer.ax_tools import require_helper_support
from services.desktop.computer.errors import ComputerUseError
from services.desktop.computer.screen import _current_scale
from services.desktop.permissions_helper import client as helper
from services.desktop.permissions_helper.client import AxActResult


class AxAction(StrEnum):
    PRESS = "press"
    SET_VALUE = "set_value"
    FOCUS = "focus"
    SHOW_MENU = "show_menu"
    PERFORM_ACTION = "perform_action"
    SELECT_TEXT = "select_text"


class SelectionType(StrEnum):
    TEXT = "text"
    CURSOR_BEFORE = "cursor_before"
    CURSOR_AFTER = "cursor_after"


ACTIONS = tuple(action.value for action in AxAction)
_MAX_VALUE_CHARS = 10_000
_ID_RE = re.compile(r"^e(\d+)$")
_UNANSWERED_NOTE = (
    "the app did not answer in time; the action may still have run — re-read with ax_tree"
)


@dataclass(frozen=True)
class AxRequest:
    sid: int
    action: AxAction
    value: str | None = None
    native_action: str | None = None
    text: str | None = None
    prefix: str | None = None
    suffix: str | None = None
    selection_type: SelectionType | None = None


def _string(args: dict[str, Any], key: str, *, nonempty: bool = False) -> str:
    value = args.get(key)
    if not isinstance(value, str):
        raise ComputerUseError(f"{key} must be a string")
    if nonempty and not value:
        raise ComputerUseError(f"{key} must not be empty")
    if len(value) > _MAX_VALUE_CHARS:
        raise ComputerUseError(f"{key} is longer than {_MAX_VALUE_CHARS} characters")
    return value


def _args(args: dict[str, Any]) -> AxRequest:
    element_id = args.get("id")
    match = _ID_RE.fullmatch(element_id) if isinstance(element_id, str) else None
    if match is None:
        raise ComputerUseError("id must be an element id like 'e12'")
    try:
        action = AxAction(args.get("action"))
    except (ValueError, TypeError):
        raise ComputerUseError(f"action must be one of {', '.join(ACTIONS)}") from None
    allowed = {
        AxAction.SET_VALUE: {"value"},
        AxAction.PERFORM_ACTION: {"native_action"},
        AxAction.SELECT_TEXT: {"text", "prefix", "suffix", "selection_type"},
    }.get(action, set())
    for key in ("value", "native_action", "text", "prefix", "suffix", "selection_type"):
        if key not in allowed and key in args and (key != "value" or args[key] is not None):
            raise ComputerUseError(f"{action} takes no {key}")
    sid = int(match.group(1))
    if action is AxAction.SET_VALUE:
        return AxRequest(sid, action, value=_string(args, "value"))
    if action is AxAction.PERFORM_ACTION:
        return AxRequest(sid, action, native_action=_string(args, "native_action", nonempty=True))
    if action is AxAction.SELECT_TEXT:
        return _selection_request(sid, args)
    return AxRequest(sid, action)


def _selection_request(sid: int, args: dict[str, Any]) -> AxRequest:
    text = _string(args, "text", nonempty=True)
    prefix = _string(args, "prefix") if "prefix" in args else None
    suffix = _string(args, "suffix") if "suffix" in args else None
    try:
        selection = SelectionType(args.get("selection_type", SelectionType.TEXT))
    except (ValueError, TypeError):
        raise ComputerUseError(
            "selection_type must be text, cursor_before or cursor_after"
        ) from None
    return AxRequest(
        sid, AxAction.SELECT_TEXT, text=text, prefix=prefix, suffix=suffix, selection_type=selection
    )


_VISUAL_ID_RE = re.compile(r"^px:(\d+)$")


def _press_visual(args: dict[str, Any], session: AxSession) -> dict[str, Any]:
    """`px:N` (a visual-only text from `ax_tree(include_ocr_gap)`): the only
    thing that can be done with it is a click at its center."""
    match = _VISUAL_ID_RE.match(str(args["id"]))
    if match is None:
        raise ComputerUseError(f"id must look like 'px:3', got {args['id']!r}")
    if args.get("action") != AxAction.PRESS or any(
        key in args and (key != "value" or args[key] is not None)
        for key in ("value", "native_action", "text", "prefix", "suffix", "selection_type")
    ):
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


def _attempt(table: AxIdTable, request: AxRequest) -> AxActResult | None:
    """One try through the element's current raw id; None when it has none."""
    raw = table.entry(request.sid).raw
    if raw is None:
        return None
    if request.action is AxAction.PERFORM_ACTION:
        result = helper.ax_act(table.app, raw, request.action, native_action=request.native_action)
    elif request.action is AxAction.SELECT_TEXT:
        result = helper.ax_act(
            table.app,
            raw,
            request.action,
            text=request.text,
            prefix=request.prefix,
            suffix=request.suffix,
            selection_type=request.selection_type,
        )
    else:
        result = helper.ax_act(table.app, raw, request.action, value=request.value)
    return None if result.get("stale") else result


def ax_act_tool(
    args: dict[str, Any], _agent_id: int, scale: float | None, session: AxSession
) -> dict[str, Any]:
    """`ax_act`: an AX action or text selection on an `ax_tree` element."""
    if str(args.get("id", "")).startswith("px:"):
        return _press_visual(args, session)
    request = _args(args)
    sid, action = request.sid, request.action
    table = session.current()
    table.entry(sid)  # unknown ids fail before any helper round trip
    capability = (
        "ax_act_v2" if action in (AxAction.PERFORM_ACTION, AxAction.SELECT_TEXT) else "ax_act_v1"
    )
    require_helper_support(capability, "ax_act")
    result = _attempt(table, request)
    if result is None:
        raise ComputerUseError(
            f"element e{sid} is gone or changed in {table.app}: call ax_tree again"
        )
    out: dict[str, Any] = {
        "element": f"e{sid}",
        "action": action,
        "completed": result["completed"],
    }
    if action is AxAction.PERFORM_ACTION:
        out["native_action"] = request.native_action
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
            "show_menu; perform_action (requires native_action exactly as reported on the "
            "element in ax_tree); select_text (requires nonempty text, exact case-sensitive "
            "substring in the current editable value, optional immediately adjacent prefix "
            "and suffix to disambiguate; zero or multiple matches fail). selection_type "
            "chooses text, cursor_before or cursor_after. Secure fields and elements "
            "without a settable selected-text range fail. Text and context are never "
            "echoed or logged. If the element or its original window is gone or changed "
            "since ax_tree you get an "
            "error — call ax_tree again, never guess an id. A px:N id (visual-only text from "
            "ax_tree include_ocr_gap) accepts only action=press, a foreground desktop click "
            "at its center. Returns the element's "
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
                "value": {"type": "string", "maxLength": _MAX_VALUE_CHARS},
                "native_action": {"type": "string", "minLength": 1, "maxLength": _MAX_VALUE_CHARS},
                "text": {"type": "string", "minLength": 1, "maxLength": _MAX_VALUE_CHARS},
                "prefix": {"type": "string", "maxLength": _MAX_VALUE_CHARS},
                "suffix": {"type": "string", "maxLength": _MAX_VALUE_CHARS},
                "selection_type": {
                    "type": "string",
                    "enum": [kind.value for kind in SelectionType],
                    "default": SelectionType.TEXT,
                },
                "task_id": {"type": "integer"},
                "priority": {"type": "string", "enum": ["normal", "high"], "default": "normal"},
            },
            "required": ["id", "action"],
        },
    },
]
