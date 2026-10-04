"""Vision gap-filling for `ax_tree(include_ocr_gap=true)`.

An accessibility tree misses what an app paints itself (custom controls,
canvas text, Electron views that expose little). This module OCRs the screen
and keeps only the text the accessibility tree does not already cover, as
"visual-only" entries numbered `px:N` — the same shape of fusion Microsoft's
UFO2 uses (drop vision detections that overlap UI Automation controls, keep
the rest). `px:` entries are click-only: `ax_act(id="px:N", action="press")`
clicks the text center; they carry no element semantics.

Selection is a pure function of the OCR boxes and the frames the accessibility
tree covers; reading the screen is the only I/O and fails soft (the tree is
still returned, with the error beside it), like `snapshot(include_ocr)`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import services.desktop.computer.ocr as ocr_mod
from services.desktop.computer.screen import _capture_screen

MAX_VISUAL = 40
Frame = tuple[float, float, float, float]  # x, y, w, h


@dataclass(frozen=True)
class VisualBox:
    text: str
    cx: int
    cy: int
    w: int
    h: int


@dataclass(frozen=True)
class GapRead:
    """One screen read: OCR items (physical pixels), the measured scale, and
    why OCR failed when it did."""

    items: list[dict[str, Any]]
    scale: float
    error: str | None


def read_screen(agent_id: int) -> GapRead:
    """Capture the screen and OCR it; an OCR failure is reported, not raised."""
    path, _size, scale, _pixels = _capture_screen(agent_id)
    try:
        return GapRead(ocr_mod.ocr_image(path), scale, None)
    except ocr_mod.OcrError as e:
        return GapRead([], scale, str(e))


def _inside(x: float, y: float, frame: Frame) -> bool:
    fx, fy, fw, fh = frame
    return fx <= x <= fx + fw and fy <= y <= fy + fh


def visual_only(
    items: list[dict[str, Any]],
    covers: list[Frame],
    window: Frame,
    scale: float,
    limit: int = MAX_VISUAL,
) -> tuple[list[VisualBox], int]:
    """OCR text inside `window` whose center no cover frame contains, in reading
    order, capped at `limit`; the second value counts those cut by the cap.

    `items` are physical pixels (the OCR space); `covers` and `window` are the
    accessibility tree's logical points, so each box center is converted before
    the test."""
    seen: set[tuple[str, int, int]] = set()
    found: list[VisualBox] = []
    for item in items:
        text = str(item["text"]).strip()
        if not text:
            continue
        cx = float(item["x"]) + float(item["w"]) / 2
        cy = float(item["y"]) + float(item["h"]) / 2
        lx, ly = cx / scale, cy / scale
        if not _inside(lx, ly, window) or any(_inside(lx, ly, c) for c in covers):
            continue
        key = (text, round(cx), round(cy))
        if key in seen:
            continue
        seen.add(key)
        found.append(
            VisualBox(text, round(cx), round(cy), round(float(item["w"])), round(float(item["h"])))
        )
    found.sort(key=lambda b: (b.cy, b.cx))
    return found[:limit], max(0, len(found) - limit)


def render_visual(boxes: list[VisualBox], hidden: int) -> str:
    """The `px:` block appended below the element tree."""
    lines = ["visual-only text (not in the accessibility tree; click via ax_act press):"]
    for number, box in enumerate(boxes, start=1):
        text = box.text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
        lines.append(f'[px:{number}] text "{text[:80]}" @{box.cx},{box.cy} {box.w}x{box.h}')
    if hidden:
        lines.append(f"... +{hidden} more visual-only text")
    return "\n".join(lines)
