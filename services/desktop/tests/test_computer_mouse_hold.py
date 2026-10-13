"""Drag options and the press-and-hold tools through MCP dispatch and the helper's JSON client."""

from __future__ import annotations

import json
from typing import Any, cast

import jsonschema
import pytest

from base.log.tests.log_records import loguru_records as loguru_records

from .test_computer_input import REGION, Desktop
from .test_computer_input import desktop as desktop

DRAG = {"start_x": 20, "start_y": 40, "end_x": 200, "end_y": 100}


def _result(response: Any) -> dict[str, Any]:
    assert response["ok"] is True, response
    return json.loads(cast(dict[str, Any], response["result"])["content"][0]["text"])


def _methods(desktop: Desktop) -> list[str]:
    return [req["method"] for req in desktop.requests]


def _sent(desktop: Desktop, method: str) -> list[dict[str, Any]]:
    return [req for req in desktop.requests if req["method"] == method]


async def test_drag_carries_button_and_modifiers_to_the_helper(desktop: Desktop) -> None:
    args = {**DRAG, "button": "right", "modifiers": ["shift", "cmd"]}
    result = _result(await desktop.call("drag", args))
    assert desktop.action("drag") == {
        "id": 1,
        "method": "drag",
        "start_x": 10,
        "start_y": 20,
        "end_x": 100,
        "end_y": 50,
        "button": "right",
        "modifiers": ["shift", "cmd"],
    }
    assert result["button"] == "right"
    # A plain drag still sends nothing the helper did not always take.
    await desktop.call("drag", DRAG)
    assert set(desktop.action("drag")) == {"id", "method", "start_x", "start_y", "end_x", "end_y"}


async def test_drag_options_need_a_current_helper_but_a_plain_drag_does_not(
    desktop: Desktop,
) -> None:
    desktop.hold_capable = False
    response = await desktop.call("drag", {**DRAG, "modifiers": ["alt"]})
    assert response["ok"] is False and "mouse_hold_v1" in response["error"]
    assert not _sent(desktop, "drag")
    assert (await desktop.call("drag", DRAG))["ok"] is True


@pytest.mark.parametrize(
    "extra",
    [{"button": "purple"}, {"button": 1}, {"modifiers": ["super"]}, {"modifiers": ["cmd", "cmd"]}],
)
async def test_drag_rejects_bad_options_before_the_helper(
    desktop: Desktop, extra: dict[str, Any]
) -> None:
    response = await desktop.call("drag", {**DRAG, **extra})
    assert response["ok"] is False
    assert not _sent(desktop, "drag")


async def test_mouse_down_converts_the_position_and_passes_options(desktop: Desktop) -> None:
    response = await desktop.call(
        "mouse_down",
        {"x": 40, "y": -20, "button": "middle", "modifiers": ["ctrl"], "task_id": 9},
    )
    assert _result(response)["pressed"] == {"x": 20, "y": -10}
    assert desktop.action("mouse_down") == {
        "id": 1,
        "method": "mouse_down",
        "x": 20,
        "y": -10,
        "button": "middle",
        "modifiers": ["ctrl"],
    }
    assert desktop.daemon._pointer == (40, -20)
    assert desktop.daemon._screen.holder == 7
    action = next(e for e in desktop.events if e.event_name == "computer_action")
    assert action.attributes["coords"] == "40,-20,middle" and action.attributes["task_id"] == 9


async def test_mouse_down_without_a_position_presses_at_the_pointer(desktop: Desktop) -> None:
    desktop.daemon._pointer = (500, 600)
    assert (await desktop.call("mouse_down", {}))["ok"] is True
    assert desktop.action("mouse_down") == {"id": 1, "method": "mouse_down", "button": "left"}
    assert desktop.daemon._pointer == (500, 600)
    action = next(e for e in desktop.events if e.event_name == "computer_action")
    assert action.attributes["coords"] == "pointer,left"


async def test_mouse_down_and_up_convert_region_frame_coordinates(desktop: Desktop) -> None:
    await desktop.call("mouse_down", {"x": 40, "y": 20, "frame": REGION})
    assert (desktop.action("mouse_down")["x"], desktop.action("mouse_down")["y"]) == (-80, 40)
    await desktop.call("mouse_up", {"x": 60, "y": 30, "frame": REGION})
    assert (desktop.action("mouse_up")["x"], desktop.action("mouse_up")["y"]) == (-70, 45)
    # A frame-relative position never replaces the tracked whole-screen pointer.
    assert desktop.daemon._pointer is None


@pytest.mark.parametrize(
    "tool,args",
    [
        ("mouse_down", {"x": 1}),
        ("mouse_down", {"y": 1}),
        ("mouse_down", {"frame": REGION}),
        ("mouse_down", {"x": 1, "y": 2, "button": "purple"}),
        ("mouse_down", {"x": 1, "y": 2, "modifiers": ["shift", "shift"]}),
        ("mouse_down", {"x": True, "y": 2}),
        ("mouse_down", {"x": float("inf"), "y": 2}),
        ("mouse_down", {"x": 1, "y": 2, "duration_ms": 5}),
        ("mouse_up", {"x": 1}),
        ("mouse_up", {"frame": REGION}),
        ("mouse_up", {"button": "purple"}),
        ("mouse_up", {"modifiers": ["shift"]}),
        ("mouse_up", {"x": "1", "y": 2}),
    ],
)
async def test_invalid_mouse_inputs_stop_before_the_helper(
    desktop: Desktop, tool: str, args: dict[str, Any]
) -> None:
    response = await desktop.call(tool, args)
    assert response["ok"] is False
    assert not any(method.startswith("mouse_") for method in _methods(desktop))
    assert desktop.daemon._mouse_holder is None
    # The refusal is audited too, whatever half of a position the call carried.
    assert desktop.events[-1].attributes["outcome"] == "error"


@pytest.mark.parametrize("tool", ["mouse_down", "mouse_up"])
async def test_old_helper_cannot_silently_ignore_the_hold(desktop: Desktop, tool: str) -> None:
    desktop.hold_capable = False
    response = await desktop.call(tool, {"x": 1, "y": 2})
    assert response["ok"] is False and "mouse_hold_v1" in response["error"]
    assert not any(method.startswith("mouse_") for method in _methods(desktop))
    assert desktop.daemon._mouse_holder is None


async def test_helper_refusals_reach_the_model_and_the_audit(desktop: Desktop) -> None:
    desktop.refusals["mouse_up"] = (
        "mouse button left is not held by Ava; mouse_up needs a mouse_down"
    )
    response = await desktop.call("mouse_up", {})
    assert response["ok"] is False and "not held by Ava" in response["error"]
    action = next(e for e in desktop.events if e.event_name == "computer_action")
    assert action.attributes["outcome"] == "error"
    desktop.refusals.clear()
    desktop.refusals["mouse_down"] = "mouse button left is already held by Ava; call mouse_up first"
    response = await desktop.call("mouse_down", {})
    assert response["ok"] is False and "already held" in response["error"]


async def test_the_daemon_remembers_who_holds_a_button(desktop: Desktop) -> None:
    await desktop.call("mouse_down", {"x": 2, "y": 2})
    assert desktop.daemon._mouse_holder == 7
    await desktop.call("mouse_up", {})
    assert desktop.daemon._mouse_holder is None


async def test_an_auto_release_notice_reaches_the_model_and_clears_the_holder(
    desktop: Desktop,
) -> None:
    await desktop.call("mouse_down", {"x": 2, "y": 2})
    notice = {
        "button": "left",
        "reason": "idle_timeout",
        "x": 1.0,
        "y": 1.0,
        "held_seconds": 30.1,
        "message": "mouse button left was released automatically after 30.1 s",
    }
    desktop.notices = [notice]
    result = _result(await desktop.call("move", {"x": 10, "y": 10}))
    assert result["auto_released"] == [notice]
    assert desktop.daemon._mouse_holder is None
    assert "auto_released" not in _result(await desktop.call("move", {"x": 10, "y": 10}))


async def test_release_control_lets_go_of_a_held_button(desktop: Desktop) -> None:
    await desktop.call("mouse_down", {"x": 2, "y": 2})
    result = _result(await desktop.call("release_control", {}))
    assert result["released"] == 7 and result["mouse_released"] == ["left"]
    assert _sent(desktop, "mouse_release_all") == [{"id": 1, "method": "mouse_release_all"}]
    assert desktop.held is None and desktop.daemon._mouse_holder is None
    # With nothing held it asks the helper nothing.
    await desktop.call("move", {"x": 1, "y": 1})
    desktop.requests.clear()
    result = _result(await desktop.call("release_control", {}))
    assert "mouse_released" not in result
    assert "mouse_release_all" not in _methods(desktop)


async def test_a_failed_release_is_loud_and_retried(
    desktop: Desktop, loguru_records: list[dict[str, Any]]
) -> None:
    await desktop.call("mouse_down", {"x": 2, "y": 2})
    desktop.refusals["mouse_release_all"] = "helper stuck"
    result = _result(await desktop.call("release_control", {}))
    assert result["released"] == 7 and "helper stuck" in result["mouse_release_error"]
    assert desktop.daemon._mouse_holder == 7, "the holder stays recorded so the next call retries"
    assert any(
        r["level"].name == "ERROR" and "helper stuck" in r["message"] for r in loguru_records
    )
    action = [e for e in desktop.events if e.attributes["action"] == "release_control"][-1]
    assert (
        action.attributes["outcome"] == "error"
        and "mouse not released" in action.attributes["error"]
    )
    desktop.refusals.clear()
    await desktop.call("release_control", {"force": True})
    assert desktop.held is None and desktop.daemon._mouse_holder is None


async def test_a_refused_release_control_still_releases_the_holders_button(
    desktop: Desktop,
) -> None:
    await desktop.call("mouse_down", {"x": 2, "y": 2})
    await desktop.daemon._screen.release(None)  # the screen moved on without this agent
    response = await desktop.call("release_control", {})
    assert response["ok"] is False and "not the screen holder" in response["error"]
    assert "mouse button you held was released" in response["error"]
    assert desktop.held is None and desktop.daemon._mouse_holder is None


async def test_release_control_of_another_agent_does_not_touch_the_button(
    desktop: Desktop,
) -> None:
    await desktop.call("mouse_down", {"x": 2, "y": 2})
    other = await desktop.daemon._dispatch(
        {"id": 5, "method": "call_tool", "tool": "release_control", "args": {}, "agent_id": 8}
    )
    assert other["ok"] is False
    assert desktop.held == "left" and "mouse_release_all" not in _methods(desktop)


async def test_a_new_screen_holder_never_inherits_a_pressed_button(desktop: Desktop) -> None:
    await desktop.call("mouse_down", {"x": 2, "y": 2})
    # The first agent's lease runs out; another agent takes the screen and acts.
    desktop.daemon._screen._lease_until = 0.0
    response = await desktop.daemon._dispatch(
        {
            "id": 6,
            "method": "call_tool",
            "tool": "move",
            "args": {"x": 4, "y": 4},
            "agent_id": 8,
        }
    )
    assert response["ok"] is True
    methods = _methods(desktop)
    assert methods.index("mouse_release_all") < methods.index("move")
    assert desktop.held is None and desktop.daemon._mouse_holder is None
    # The same agent carrying on is not a takeover.
    desktop.requests.clear()
    await desktop.call("mouse_down", {"x": 1, "y": 1})
    await desktop.call("move", {"x": 3, "y": 3})
    assert "mouse_release_all" not in _methods(desktop)


async def test_schemas_are_strict_and_state_the_release_contract(desktop: Desktop) -> None:
    listed = await desktop.daemon._dispatch({"id": 1, "method": "list_tools"})
    assert listed["ok"] is True
    tools = {tool["name"]: tool for tool in cast(list[dict[str, Any]], listed["result"])}
    down, up = tools["mouse_down"]["input_schema"], tools["mouse_up"]["input_schema"]
    jsonschema.validate({"x": 1, "y": 2, "button": "right", "modifiers": ["alt"]}, down)
    jsonschema.validate({}, down)
    jsonschema.validate({"x": 1, "y": 2, "button": "middle"}, up)
    for schema, bad in (
        (down, {"x": 1, "y": 2, "button": "purple"}),
        (down, {"unknown": 1}),
        (up, {"modifiers": ["shift"]}),
        (up, {"button": "purple"}),
    ):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(bad, schema)
    jsonschema.validate(
        {**DRAG, "button": "right", "modifiers": ["shift"]}, tools["drag"]["input_schema"]
    )
    description = tools["mouse_down"]["description"]
    for phrase in (
        "MUST be released",
        "try/finally",
        "30 seconds",
        "auto_released",
        "top-left corner",
    ):
        assert phrase in description
    assert "mouse_up" in tools["drag"]["description"]
    assert "held" in tools["release_control"]["description"]
