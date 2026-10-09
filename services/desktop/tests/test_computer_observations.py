"""Observation and app focus contracts through MCP and a real Unix socket."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from ..computer import execute
from ..computer.protocol import Response
from .test_computer_input import Desktop
from .test_computer_input import desktop as desktop


def payload(response: Response) -> dict[str, Any]:
    assert response["ok"] is True, response
    return json.loads(cast(dict[str, Any], response["result"])["content"][0]["text"])


async def test_region_snapshot_preserves_global_scale_and_ocr_cache(
    desktop: Desktop, monkeypatch: pytest.MonkeyPatch
) -> None:
    desktop.daemon._scale = 1
    desktop.daemon._ocr_cache["items"] = [{"text": "previous whole-screen text"}]

    def ocr(_path: str | Path) -> list[dict[str, float | str]]:
        return [{"text": "region text", "x": 10, "y": 20}]

    monkeypatch.setattr(execute.ocr_mod, "ocr_image", ocr)
    result = payload(
        await desktop.call(
            "snapshot", {"region": {"x": 100, "y": 150, "w": 200, "h": 100}, "include_ocr": True}
        )
    )
    assert result["source"] == "region"
    assert result["frame"] == {
        "coordinate_space": "region_pixels",
        "origin": {"x": 100, "y": 150},
        "scale": 2,
        "pixels": {"width": 400, "height": 200},
    }
    assert desktop.daemon._scale == 1
    assert desktop.daemon._ocr_cache["items"] == [{"text": "previous whole-screen text"}]
    assert result["ocr"][0]["text"] == "region text"
    assert (await desktop.call("click", {"x": 40, "y": 20, "frame": result["frame"]}))["ok"] is True
    assert (desktop.action("click")["x"], desktop.action("click")["y"]) == (120, 160)


async def test_window_snapshot_carries_identity_but_refuses_global_pointer(
    desktop: Desktop,
) -> None:
    result = payload(await desktop.call("snapshot", {"target": {"pid": 77, "window_id": 42}}))
    assert result["source"] == "window"
    assert result["frame"]["target"] == {"pid": 77, "window_id": 42}
    assert result["frame"]["coordinate_space"] == "window_pixels"
    assert result["frame"]["origin"] == {"x": 30, "y": 40}
    assert desktop.action("screencapture_window")["pid"] == 77
    response = await desktop.call("click", {"x": 1, "y": 1, "frame": result["frame"]})
    assert response["ok"] is False and "unsupported" in response["error"]
    assert desktop.daemon._scale == 2


async def test_inventory_keeps_nullable_metadata_and_exact_app_selector(desktop: Desktop) -> None:
    result = payload(await desktop.call("list_apps", {}))
    assert result["apps"] == [{"pid": 77, "name": None, "bundle_id": "org.test.target"}]
    result = payload(await desktop.call("list_windows", {"app": "org.test.target"}))
    assert result["windows"][0]["title"] is None and result["windows"][0]["on_screen"] is False
    assert desktop.action("list_windows")["app"] == "org.test.target"


@pytest.mark.parametrize("target", [{"pid": 77}, {"bundle_id": "org.test.target"}])
async def test_focus_app_is_explicit_and_forwards_one_identity(
    desktop: Desktop, target: dict[str, Any]
) -> None:
    result = payload(await desktop.call("focus_app", {"target": target}))
    assert result == {"focused": True, "pid": 77}
    assert desktop.action("focus_app") == {"id": 1, "method": "focus_app", **target}


@pytest.mark.parametrize(
    "tool,args",
    [
        (
            "snapshot",
            {"region": {"x": 0, "y": 0, "w": 10, "h": 10}, "target": {"pid": 77, "window_id": 42}},
        ),
        ("snapshot", {"region": {"x": 0, "y": 0, "w": 0, "h": 10}}),
        ("snapshot", {"region": {"x": 999, "y": 0, "w": 10, "h": 10}}),
        ("snapshot", {"region": {"x": 0, "y": 0, "w": 10, "h": 10}, "include_ax": True}),
        ("snapshot", {"target": {"pid": True, "window_id": 42}}),
        ("snapshot", {"target": {"pid": 77, "window_id": 0}}),
        ("focus_app", {"target": {"pid": 77, "bundle_id": "org.test.target"}}),
        ("focus_app", {"target": {"pid": True}}),
        ("focus_app", {"target": {"bundle_id": " "}}),
        ("list_windows", {"app": True}),
    ],
)
async def test_observation_selectors_fail_before_capture_or_focus(
    desktop: Desktop, tool: str, args: dict[str, Any]
) -> None:
    assert (await desktop.call(tool, args))["ok"] is False
    assert not any(
        req["method"]
        in {"screencapture_region", "screencapture_window", "focus_app", "list_windows"}
        for req in desktop.requests
    )


async def test_window_identity_and_focus_confirmation_failures_are_explicit(
    desktop: Desktop,
) -> None:
    desktop.denied = "window target is stale or does not belong to the requested PID"
    response = await desktop.call("snapshot", {"target": {"pid": 77, "window_id": 42}})
    assert response["ok"] is False and "stale" in response["error"]
    desktop.denied = (
        "app activation was requested but focused PID could not be confirmed; capture again"
    )
    response = await desktop.call("focus_app", {"target": {"pid": 77}})
    assert response["ok"] is False and "could not be confirmed" in response["error"]
