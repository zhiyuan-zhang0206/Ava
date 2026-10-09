"""MCP schema, daemon coordination and real helper socket input contracts."""

from __future__ import annotations

import json
import socket
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import jsonschema
import pytest

from base.db import Database

from ..computer.config import ComputerUseConfig
from ..computer.mcp_daemon import ComputerMcpDaemon
from ..computer.protocol import Response
from ..permissions_helper import client


class Desktop:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.events: list[Any] = []
        self.capable = True
        self.denied: str | None = None
        self.daemon = ComputerMcpDaemon(
            ComputerUseConfig(30, 0.01, 30, 60, 1), Database.from_settings(), sock="/unused"
        )
        self.daemon._scale = 2

    def respond(self, req: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(req)
        method = req["method"]
        if method == "ping":
            result: dict[str, Any] = {"pong": True, "native_input_v1": self.capable}
        elif method == "frontmost_app":
            result = {"app": "InertTarget"}
        elif method == "screen_size":
            result = {"x": 0, "y": 0, "w": 1000, "h": 800, "scale": 2}
        elif method == "cursor_position":
            result = {"x": -8, "y": 33}
        elif self.denied:
            return {"ok": False, "error": self.denied}
        elif method in {
            "list_apps",
            "list_windows",
            "focus_app",
            "screencapture_region",
            "screencapture_window",
        }:
            result = self.observe(req)
        elif method == "click":
            count = req.get("click_count", 2 if req.get("double") else 1)
            result = {
                "clicked": {"x": req["x"], "y": req["y"]},
                "double": count == 2,
                "button": req.get("button", "left"),
                "click_count": count,
            }
        elif method == "move":
            result = {"moved": {"x": req["x"], "y": req["y"]}}
        elif method == "scroll":
            result = {"scrolled": req["dy"], "dx": req.get("dx", 0)}
        elif method == "key":
            result = {
                "key": req["code"],
                "cmd": req.get("cmd", False) or "cmd" in req.get("modifiers", []),
            }
        elif method == "drag":
            result = {
                "start": {"x": req["start_x"], "y": req["start_y"]},
                "end": {"x": req["end_x"], "y": req["end_y"]},
            }
        else:
            raise AssertionError(method)
        return {"ok": True, "result": result}

    def observe(self, req: dict[str, Any]) -> dict[str, Any]:
        method = req["method"]
        if method == "list_apps":
            result = {"apps": [{"pid": 77, "name": None, "bundle_id": "org.test.target"}]}
        elif method == "list_windows":
            result = {
                "windows": [
                    {
                        "window_id": 42,
                        "pid": 77,
                        "owner": None,
                        "title": None,
                        "x": 30,
                        "y": 40,
                        "w": 100,
                        "h": 50,
                        "on_screen": False,
                    }
                ]
            }
        elif method == "focus_app":
            result = {"focused": True, "pid": req.get("pid", 77)}
        elif method in {"screencapture_region", "screencapture_window"}:
            width, height = (
                (req["w"] * 2, req["h"] * 2) if method == "screencapture_region" else (200, 100)
            )
            Path(req["path"]).write_bytes(
                b"\x89PNG\r\n\x1a\n"
                + b"\x00\x00\x00\x0dIHDR"
                + width.to_bytes(4, "big")
                + height.to_bytes(4, "big")
            )
            result = {"path": req["path"], "bytes": 24}
            if method == "screencapture_window":
                result.update(
                    {"origin": {"x": 30, "y": 40}, "scale": 2, "width": width, "height": height}
                )
        else:
            raise AssertionError(method)
        return result

    async def call(self, tool: str, args: dict[str, Any]) -> Response:
        return await self.daemon._dispatch(
            {"id": 1, "method": "call_tool", "tool": tool, "args": args, "agent_id": 7}
        )

    def action(self, method: str) -> dict[str, Any]:
        return next(req for req in reversed(self.requests) if req["method"] == method)


@pytest.fixture
def desktop(monkeypatch: pytest.MonkeyPatch) -> Iterator[Desktop]:
    target = Desktop()
    errors: list[BaseException] = []
    stopped = threading.Event()

    def record(_db: object, event: Any) -> None:
        target.events.append(event)

    monkeypatch.setattr("base.agents.impersonation.manifest.emit_recorded_central_event", record)
    with tempfile.TemporaryDirectory(prefix="ava-input-", dir="/tmp") as directory:
        path = Path(directory) / "helper.sock"
        monkeypatch.setattr(client, "permissions_helper_socket", lambda: path)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(path))
            listener.listen(1)
            listener.settimeout(0.1)

            def serve() -> None:
                while not stopped.is_set():
                    try:
                        connection, _ = listener.accept()
                    except TimeoutError:
                        continue
                    try:
                        with connection, connection.makefile("rb") as stream:
                            response = target.respond(json.loads(stream.readline()))
                            connection.sendall(json.dumps(response).encode() + b"\n")
                    except BaseException as exc:
                        errors.append(exc)
                        return

            worker = threading.Thread(target=serve, daemon=True)
            worker.start()
            try:
                yield target
            finally:
                stopped.set()
                worker.join(timeout=3)
                assert not worker.is_alive()
                assert not errors, errors


@pytest.mark.parametrize("button,count", [("left", 1), ("right", 2), ("middle", 3)])
async def test_click_wire_coordinates_options_audit_and_session(
    desktop: Desktop, button: str, count: int
) -> None:
    args = {
        "x": -20,
        "y": 40,
        "button": button,
        "click_count": count,
        "modifiers": ["shift", "cmd"],
        "duration_ms": 25,
        "task_id": 42,
    }
    response = await desktop.call("click", args)
    assert response["ok"] is True
    assert desktop.action("click") == {
        "id": 1,
        "method": "click",
        "x": -10,
        "y": 20,
        "double": False,
        **({"button": button} if button != "left" else {}),
        "click_count": count,
        "modifiers": ["shift", "cmd"],
        "duration_ms": 25,
    }
    assert desktop.daemon._screen.holder == 7
    action = next(event for event in desktop.events if event.event_name == "computer_action")
    assert action.attributes["coords"] == "-20,40" and action.attributes["task_id"] == 42
    assert any(event.event_name == "computer_session_start" for event in desktop.events)


async def test_key_hold_modifiers_and_raw_code(desktop: Desktop) -> None:
    response = await desktop.call(
        "key", {"key": "a", "modifiers": ["ctrl", "alt", "cmd"], "duration_ms": 100}
    )
    assert response["ok"] is True
    request = desktop.action("key")
    assert request["code"] == 0 and request["duration_ms"] == 100
    assert request["modifiers"] == ["ctrl", "alt", "cmd"]
    assert (await desktop.call("key", {"keycode": 65535}))["ok"] is True
    assert desktop.action("key")["code"] == 65535


async def test_move_cursor_and_horizontal_scroll_use_live_position(desktop: Desktop) -> None:
    assert (await desktop.call("move", {"x": 80, "y": -20}))["ok"] is True
    assert desktop.action("move")["x"] == 40
    response = await desktop.call("cursor_position", {})
    assert response["ok"] is True
    payload = cast(dict[str, Any], response["result"])
    assert json.loads(payload["content"][0]["text"]) == {"x": -16, "y": 66, "scale": 2}
    assert (await desktop.call("scroll", {"dx": 17, "modifiers": ["alt"]}))["ok"] is True
    assert desktop.action("scroll") == {
        "id": 1,
        "method": "scroll",
        "x": -8,
        "y": 33,
        "dy": 0,
        "dx": 17,
        "modifiers": ["alt"],
    }


REGION = {
    "coordinate_space": "region_pixels",
    "origin": {"x": -100, "y": 30},
    "scale": 2,
    "pixels": {"width": 200, "height": 100},
}


@pytest.mark.parametrize(
    "tool,args,expected",
    [
        ("click", {"x": 40, "y": 20}, {"x": -80, "y": 40}),
        ("move", {"x": 60, "y": 30}, {"x": -70, "y": 45}),
        ("scroll", {"x": 10, "y": 10, "dx": 3}, {"x": -95, "y": 35}),
        (
            "drag",
            {"start_x": 0, "start_y": 0, "end_x": 100, "end_y": 50},
            {"start_x": -100, "start_y": 30, "end_x": -50, "end_y": 55},
        ),
    ],
)
async def test_region_frame_converts_both_axes_without_global_state(
    desktop: Desktop, tool: str, args: dict[str, Any], expected: dict[str, Any]
) -> None:
    desktop.daemon._pointer = (500, 600)
    assert (await desktop.call(tool, {**args, "frame": REGION}))["ok"] is True
    request = desktop.action(tool)
    assert all(request[key] == value for key, value in expected.items())
    assert desktop.daemon._scale == 2 and desktop.daemon._pointer == (500, 600)


@pytest.mark.parametrize(
    "tool,args",
    [
        ("click", {"x": True, "y": 1}),
        ("click", {"x": float("inf"), "y": 1}),
        ("click", {"x": "1", "y": 1}),
        ("click", {"x": 1, "y": 1, "button": "bad"}),
        ("click", {"x": 1, "y": 1, "click_count": True}),
        ("click", {"x": 1, "y": 1, "click_count": 4}),
        ("click", {"x": 1, "y": 1, "double": True, "click_count": 3}),
        ("click", {"x": 1, "y": 1, "duration_ms": 5001}),
        ("key", {"keycode": True}),
        ("key", {"keycode": 65536}),
        ("key", {"keycode": -1}),
        ("key", {"key": "a", "keycode": 0}),
        ("key", {"key": "a", "duration_ms": 10001}),
        ("key", {"key": "a", "modifiers": ["cmd", "cmd"]}),
        ("key", {"key": "a", "modifiers": ["super"]}),
        ("scroll", {"dy": True}),
        ("scroll", {"dx": 2**31}),
        ("scroll", {"dx": 1, "x": 2}),
        ("scroll", {"dx": 1, "frame": REGION}),
        ("move", {"x": 200, "y": 0, "frame": REGION}),
        ("drag", {"start_x": 0, "start_y": 0, "end_x": 200, "end_y": 0, "frame": REGION}),
    ],
)
async def test_invalid_inputs_stop_before_action(
    desktop: Desktop, tool: str, args: dict[str, Any]
) -> None:
    response = await desktop.call(tool, args)
    assert response["ok"] is False
    assert not any(req["method"] == tool for req in desktop.requests)


async def test_window_frame_input_refused_before_any_event(desktop: Desktop) -> None:
    frame = {**REGION, "coordinate_space": "window_pixels", "target": {"pid": 7, "window_id": 42}}
    response = await desktop.call("click", {"x": 10, "y": 20, "frame": frame})
    assert response["ok"] is False and "unsupported" in response["error"]
    assert not any(req["method"] == "click" for req in desktop.requests)


async def test_old_helper_cannot_silently_ignore_right_button(desktop: Desktop) -> None:
    desktop.capable = False
    response = await desktop.call("click", {"x": 1, "y": 2, "button": "right"})
    assert response["ok"] is False and "native_input_v1" in response["error"]
    assert not any(req["method"] == "click" for req in desktop.requests)
    assert (await desktop.call("click", {"x": 1, "y": 2}))["ok"] is True


async def test_accessibility_denial_keeps_pointer_and_audits_failure(desktop: Desktop) -> None:
    desktop.denied = "Accessibility grant missing (ax_trusted=false)"
    response = await desktop.call("move", {"x": 10, "y": 20})
    assert response["ok"] is False and "Accessibility grant missing" in response["error"]
    assert desktop.daemon._pointer is None
    action = next(event for event in desktop.events if event.event_name == "computer_action")
    assert action.attributes["outcome"] == "error"


async def test_derived_schemas_describe_options_and_reject_unknown_fields(desktop: Desktop) -> None:
    listed = await desktop.daemon._dispatch({"id": 1, "method": "list_tools"})
    assert listed["ok"] is True
    tools = cast(list[dict[str, Any]], listed["result"])
    schemas = {tool["name"]: tool["input_schema"] for tool in tools}
    jsonschema.validate(
        {"x": 1, "y": 2, "button": "middle", "click_count": 3, "modifiers": ["alt"]},
        schemas["click"],
    )
    jsonschema.validate({"dx": 5}, schemas["scroll"])
    jsonschema.validate({"keycode": 7, "duration_ms": 20}, schemas["key"])
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"x": 1, "y": 2, "unknown": 1}, schemas["move"])


async def test_named_modifier_hold_requires_current_helper(desktop: Desktop) -> None:
    desktop.capable = False
    response = await desktop.call("key", {"key": "shift"})
    assert response["ok"] is False and "native_input_v1" in response["error"]
    assert not any(req["method"] == "key" for req in desktop.requests)
    desktop.capable = True
    assert (await desktop.call("key", {"key": "shift", "duration_ms": 25}))["ok"] is True
    assert desktop.action("key")["code"] == 56
