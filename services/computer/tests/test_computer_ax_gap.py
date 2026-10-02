"""Tests for `ax_tree(include_ocr_gap)` (ax_gap.py) and px: clicks (ax_act.py).

Selection of the visual-only text is a pure function of OCR boxes and the
frames the accessibility tree covers. The tool runs against a faked helper and
a faked screen read; real OCR and a real desktop are not involved.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from services.computer import ax_gap
from services.computer.ax_gap import Frame, GapRead, VisualBox
from services.computer.errors import ComputerUseError
from services.computer.mcp_daemon import ComputerMcpDaemon
from services.permissions_helper import client as helper
from services.permissions_helper.client import AxNode, AxTreeResult

WINDOW = (0.0, 0.0, 1000.0, 800.0)


def ocr(text: str, x: float, y: float, w: float = 80.0, h: float = 20.0) -> dict[str, Any]:
    return {"text": text, "x": x, "y": y, "w": w, "h": h}


# ── pure selection ──────────────────────────────────────────────────────────


def test_text_inside_an_ax_element_is_not_visual_only() -> None:
    covers = [(10.0, 10.0, 100.0, 40.0)]  # logical points; scale 2 -> physical 20..220
    items = [ocr("Send", 30, 30), ocr("Painted", 600, 400)]
    boxes, hidden = ax_gap.visual_only(items, covers, WINDOW, 2.0)
    assert [b.text for b in boxes] == ["Painted"] and hidden == 0


def test_text_outside_the_window_blank_text_and_duplicates_are_dropped() -> None:
    items = [
        ocr("Inside", 100, 100),
        ocr("Inside", 100, 100),  # the same box twice
        ocr("  ", 200, 200),
        ocr("Elsewhere", 5000, 5000),
    ]
    boxes, _ = ax_gap.visual_only(items, [], WINDOW, 2.0)
    assert [b.text for b in boxes] == ["Inside"]


def test_results_come_in_reading_order_and_the_cap_counts_what_it_cut() -> None:
    items = [ocr("low", 100, 600), ocr("right", 400, 100), ocr("left", 100, 100)]
    boxes, hidden = ax_gap.visual_only(items, [], WINDOW, 2.0, limit=2)
    assert [b.text for b in boxes] == ["left", "right"] and hidden == 1


def test_boxes_are_reported_as_physical_pixel_centers() -> None:
    boxes, _ = ax_gap.visual_only([ocr("A", 100, 100, 60, 20)], [], WINDOW, 2.0)
    assert boxes == [VisualBox("A", 130, 110, 60, 20)]


def test_render_visual_lines() -> None:
    text = ax_gap.render_visual([VisualBox('Say "hi"', 130, 110, 60, 20)], hidden=3)
    assert text.splitlines() == [
        "visual-only text (not in the accessibility tree; click via ax_act press):",
        '[px:1] text "Say \\"hi\\"" @130,110 60x20',
        "... +3 more visual-only text",
    ]


# ── the tool ────────────────────────────────────────────────────────────────


def _node(nid: int, parent: int | None, role: str, frame: Frame, **kw: Any) -> AxNode:
    out: dict[str, Any] = {"id": nid, "fp": f"fp{nid}", "depth": 0 if parent is None else 1, "n": 0}
    out.update(role=role, x=frame[0], y=frame[1], w=frame[2], h=frame[3], **kw)
    if parent is not None:
        out["parent"] = parent
    return out  # pyright: ignore[reportReturnType]


class FakeAxHelper:
    """A window (1000x800 logical) holding one Send button at (10,20,100,40)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        nodes = [
            _node(1, None, "AXWindow", WINDOW, title="Compose", n=1),
            _node(2, 1, "AXButton", (10.0, 20.0, 100.0, 40.0), title="Send"),
        ]
        self.tree: AxTreeResult = {  # pyright: ignore[reportAssignmentType]
            "app": "Mail",
            "pid": 1,
            "windows": 1,
            "framework": "",
            "ax_enable": "n/a",
            "nodes": nodes,
            "visited": 2,
            "truncated": False,
            "timed_out": False,
            "unreadable": 0,
            "elapsed_ms": 1,
        }

    def ping(self, **kw: Any) -> dict[str, Any]:
        return {"pong": True, "ax_tree_v1": True}

    def frontmost_app(self, **kw: Any) -> dict[str, Any]:
        return {"app": "Mail"}

    def screen_size(self, **kw: Any) -> dict[str, Any]:
        return {"x": 0.0, "y": 0.0, "w": 1512.0, "h": 982.0, "scale": 2.0}

    def ax_tree(self, app: str, **kw: Any) -> AxTreeResult:
        self.calls.append(("ax_tree", {"app": app, **kw}))
        return self.tree


@pytest.fixture
def fake_ax(monkeypatch: pytest.MonkeyPatch) -> FakeAxHelper:
    fake = FakeAxHelper()
    for name in ("ping", "frontmost_app", "screen_size", "ax_tree"):
        monkeypatch.setattr(helper, name, getattr(fake, name))
    return fake


@pytest.fixture
def gap(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {
        "read": GapRead([ocr("Send", 30, 30), ocr("Canvas label", 600, 400)], 2.0, None)
    }

    def read_screen(_agent_id: int) -> GapRead:
        return state["read"]

    monkeypatch.setattr(ax_gap, "read_screen", read_screen)
    return state


class Recorder:
    def __init__(self) -> None:
        self.clicks: list[tuple[float, float]] = []

    def click(self, x: float, y: float, **kw: Any) -> dict[str, Any]:
        self.clicks.append((x, y))
        return {"clicked": {"x": x, "y": y}, "double": False}


@pytest.fixture
def clicks(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    rec = Recorder()
    monkeypatch.setattr(helper, "click", rec.click)
    return rec


async def call(
    daemon: ComputerMcpDaemon, tool: str, args: dict[str, Any] | None = None
) -> dict[str, Any]:
    resp = await daemon._dispatch(
        {"id": 1, "method": "call_tool", "tool": tool, "args": args or {}, "agent_id": 7}
    )
    if resp["ok"] is False:
        raise ComputerUseError(resp["error"])
    return json.loads(resp["result"]["content"][0]["text"])


async def test_include_ocr_gap_appends_only_the_text_the_tree_misses(
    fake_ax: FakeAxHelper, gap: dict[str, Any]
) -> None:
    out = await call(ComputerMcpDaemon(sock="/x.sock"), "ax_tree", {"include_ocr_gap": True})
    # "Send" sits inside the Send button's frame (10,20,100,40 logical, x2); the canvas label does not.
    assert '[px:1] text "Canvas label" @640,410 80x20' in out["tree"]
    assert "[px:2]" not in out["tree"] and 'text "Send"' not in out["tree"]
    assert out["visual_only"] == 1 and "ocr_gap_error" not in out


async def test_ocr_failure_is_soft_the_tree_is_still_returned(
    fake_ax: FakeAxHelper, gap: dict[str, Any]
) -> None:
    gap["read"] = GapRead([], 2.0, "swiftc not found on PATH")
    out = await call(ComputerMcpDaemon(sock="/x.sock"), "ax_tree", {"include_ocr_gap": True})
    assert (
        out["tree"].startswith("[e1] window") and out["ocr_gap_error"] == "swiftc not found on PATH"
    )


async def test_the_gap_needs_a_whole_window_read(
    fake_ax: FakeAxHelper, gap: dict[str, Any]
) -> None:
    with pytest.raises(ComputerUseError, match="drop scope"):
        await call(
            ComputerMcpDaemon(sock="/x.sock"),
            "ax_tree",
            {"include_ocr_gap": True, "scope": "e1"},
        )


async def test_enable_ax_reaches_the_helper_and_defaults_on(fake_ax: FakeAxHelper) -> None:
    daemon = ComputerMcpDaemon(sock="/x.sock")
    await call(daemon, "ax_tree")
    assert fake_ax.calls[-1][1]["enable_ax"] is True
    await call(daemon, "ax_tree", {"enable_ax": False})
    assert fake_ax.calls[-1][1]["enable_ax"] is False


async def test_a_px_press_clicks_the_text_center_in_logical_points(
    fake_ax: FakeAxHelper, gap: dict[str, Any], clicks: Recorder
) -> None:
    daemon = ComputerMcpDaemon(sock="/x.sock")
    await call(daemon, "ax_tree", {"include_ocr_gap": True})
    out = await call(daemon, "ax_act", {"id": "px:1", "action": "press"})
    assert clicks.clicks == [(320.0, 205.0)]
    assert out == {
        "element": "px:1",
        "action": "press",
        "completed": True,
        "label": "Canvas label",
        "x": 640,
        "y": 410,
    }


@pytest.mark.parametrize(
    "args",
    [
        {"id": "px:1", "action": "set_value", "value": "x"},
        {"id": "px:1", "action": "focus"},
        {"id": "px:9", "action": "press"},
    ],
)
async def test_px_elements_only_press_and_only_the_latest_ones(
    fake_ax: FakeAxHelper, gap: dict[str, Any], clicks: Recorder, args: dict[str, Any]
) -> None:
    daemon = ComputerMcpDaemon(sock="/x.sock")
    await call(daemon, "ax_tree", {"include_ocr_gap": True})
    with pytest.raises(ComputerUseError):
        await call(daemon, "ax_act", args)
    assert clicks.clicks == []


async def test_a_later_ax_tree_without_the_gap_retires_the_px_ids(
    fake_ax: FakeAxHelper, gap: dict[str, Any], clicks: Recorder
) -> None:
    daemon = ComputerMcpDaemon(sock="/x.sock")
    await call(daemon, "ax_tree", {"include_ocr_gap": True})
    await call(daemon, "ax_tree")
    with pytest.raises(ComputerUseError, match="unknown visual element px:1"):
        await call(daemon, "ax_act", {"id": "px:1", "action": "press"})
    assert clicks.clicks == []
