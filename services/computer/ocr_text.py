"""OCR, text matching, and the find_text/click_text tools for computer-mcp."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import services.computer.ocr as ocr_mod
from services.computer.errors import ComputerUseError
from services.computer.screen import _capture_screen, _to_logical
from services.permissions_helper import client as helper


def _ocr_screen(path: Path, ocr_cache: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Recognize text in a fresh capture and record it in the OCR cache.

    Strict where the snapshot tool is soft: find_text/click_text exist to act
    on recognized text, so an OCR failure here is an error, never a silent
    empty list. The cache entry (also written by snapshot include_ocr) serves
    find_text(snapshot_fresh=false), so a caller can search the exact screen
    it just saw without paying for a second capture.
    """
    try:
        items = ocr_mod.ocr_image(path)
    except ocr_mod.OcrError as e:
        raise ComputerUseError(f"ocr failed: {e}") from e
    if ocr_cache is not None:
        ocr_cache["items"] = items
    return items


def _validate_text_query(query: str, mode: str) -> None:
    """Shared argument validation for the OCR text tools.

    A blank query or an unknown match mode is a caller bug — fail fast with a
    readable error (same stance as the daemon's required-argument check).
    """
    if not query.strip():
        raise ComputerUseError("text must be a non-empty string")
    if mode not in ("contains", "exact"):
        raise ComputerUseError(f"match must be 'contains' or 'exact', got {mode!r}")


def _match_ocr_boxes(items: list[dict[str, Any]], query: str, mode: str) -> list[dict[str, Any]]:
    """OCR boxes matching ``query``, in reading order (top-to-bottom, then
    left-to-right).

    Both modes are case-insensitive — Vision echoes title case a caller may
    have typed lower, and CJK has no case to lose. Each match keeps its
    physical-pixel box and gains the center (cx/cy, the click target) and its
    0-based position in the returned ordering (the index click_text acts on).
    """
    needle = query.casefold()
    matches: list[dict[str, Any]] = []
    for item in items:
        text = str(item.get("text") or "")
        folded = text.casefold()
        hit = folded == needle if mode == "exact" else needle in folded
        if not hit:
            continue
        x, y, w, h = (float(item[k]) for k in ("x", "y", "w", "h"))
        matches.append(
            {
                "text": text,
                "x": x,
                "y": y,
                "w": w,
                "h": h,
                "cx": x + w / 2,
                "cy": y + h / 2,
            }
        )
    matches.sort(key=lambda m: (m["y"], m["x"]))
    for position, match in enumerate(matches):
        match["index"] = position
    return matches


def _find_text_tool(
    args: dict[str, Any], agent_id: int, ocr_cache: dict[str, Any] | None
) -> dict[str, Any]:
    """find_text: OCR the screen and return the boxes matching ``text``.

    Fresh capture by default. snapshot_fresh=false searches the last OCR
    result instead (the screen a snapshot include_ocr / find_text / click_text
    most recently read) — the result reports fresh:false so the caller knows
    it answers an older screen. OCR failure is an error, never an empty list:
    this tool exists to act on text.
    """
    query = str(args.get("text") or "")
    mode = str(args.get("match") or "contains")
    _validate_text_query(query, mode)
    cached_items = ocr_cache.get("items") if ocr_cache is not None else None
    if not args.get("snapshot_fresh", True) and cached_items:
        items: list[dict[str, Any]] = cached_items  # the screen the caller last saw
        fresh = False
    else:
        path, _size, _scale, _pixels = _capture_screen(agent_id)
        items = _ocr_screen(path, ocr_cache)
        fresh = True
    matches = _match_ocr_boxes(items, query, mode)
    return {
        "query": query,
        "match": mode,
        "fresh": fresh,
        "count": len(matches),
        "matches": matches,
    }


def _click_text_tool(
    args: dict[str, Any], agent_id: int, ocr_cache: dict[str, Any] | None
) -> dict[str, Any]:
    """click_text: OCR -> locate -> click, one audited action.

    Always reads the screen fresh: the click must land on what is there NOW,
    not on a cached capture the screen may have moved past. The capture also
    measures the scale this click converts with. index picks among multiple
    matches in the same reading order find_text returns; a missing match or
    an out-of-range index is a readable error — the tool never clicks blind.
    """
    query = str(args.get("text") or "")
    mode = str(args.get("match") or "contains")
    index = int(args.get("index", 0))
    _validate_text_query(query, mode)
    if index < 0:
        raise ComputerUseError("index must be >= 0")
    path, _size, scale, _pixels = _capture_screen(agent_id)
    matches = _match_ocr_boxes(_ocr_screen(path, ocr_cache), query, mode)
    if not matches:
        raise ComputerUseError(f"no on-screen text matching {query!r} (match={mode})")
    if index >= len(matches):
        raise ComputerUseError(
            f"{query!r} matched {len(matches)} box(es); index {index} is out of range"
        )
    box = matches[index]
    clicked = helper.click(
        _to_logical(box["cx"], scale),
        _to_logical(box["cy"], scale),
        double=False,
    )
    return {
        "clicked": clicked["clicked"],
        "double": clicked["double"],
        "text": box["text"],
        "x": box["cx"],
        "y": box["cy"],
        "scale": scale,
    }
