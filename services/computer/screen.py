"""Screen capture and physical<->logical coordinate space for the computer-mcp
daemon.
"""

from __future__ import annotations

import struct
from datetime import UTC, datetime
from pathlib import Path

from services.computer.errors import ComputerUseError
from services.permissions_helper import client as helper
from shared.paths import logs_dir


def _png_size(path: Path) -> tuple[int, int]:
    """Physical pixel size of a PNG from its IHDR (signature + 4-byte length +
    'IHDR' + width + height, big-endian). No image library needed."""
    with Path(path).open("rb") as f:
        head = f.read(24)
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        raise ComputerUseError(f"captured file {path} is not a PNG")
    width, height = struct.unpack(">II", head[16:24])
    return width, height


def _to_logical(value: float, scale: float) -> float:
    """Physical-pixel coordinate -> helper's logical-point space (divide by the
    measured backing scale; scale 1 (1x display) is a no-op there)."""
    return value / scale if scale > 1 else value


def _pixel_scale(pixels_w: int, logical_w: float) -> float:
    """Measure the physical->logical scale from the captured PNG itself.

    The PNG is the ground truth of the click space (IHDR width = physical
    pixels of exactly the region the caller clicks); the helper's report is
    not trusted here — a process without an AppKit event loop can hold stale
    screen objects (2026-08-30: scale 2 on a 1x display, halving every click).
    """
    if pixels_w <= 0 or logical_w <= 0:
        raise ComputerUseError(f"cannot compute screen scale from {pixels_w}px over {logical_w}pt")
    return pixels_w / logical_w


def _current_scale(measured: float | None) -> float:
    """The physical->logical scale for coordinate conversion.

    Prefers the daemon's own measurement (last snapshot, ``_pixel_scale``);
    before any snapshot, falls back to the helper's live report.
    """
    if measured is not None:
        return measured
    return float(helper.screen_size()["scale"])


def _snapshot_path(agent_id: int | None) -> Path:
    """A fresh capture path under $AVA_HOME/logs/computer/snapshots/."""
    directory = logs_dir() / "computer" / "snapshots"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    return directory / f"agent-{agent_id or 0}-{stamp}.png"


def _capture_screen(agent_id: int) -> tuple[Path, helper.ScreenSize, float, tuple[int, int]]:
    """One full-screen capture: (PNG path, helper screen report, measured
    scale, physical pixel size).

    The capture shared by snapshot and the OCR text tools — screencapture -R
    takes logical points and clips to the display; the PNG comes out at
    physical resolution (Retina 2x), reported via IHDR. Measure, don't trust:
    the PNG is the ground truth callers click against; the helper's reported
    scale can be stale (see _pixel_scale).
    """
    path = _snapshot_path(agent_id)
    size = helper.screen_size()
    helper.screencapture_region(0, 0, int(size["w"]), int(size["h"]), str(path))
    pw, ph = _png_size(path)
    return path, size, _pixel_scale(pw, size["w"]), (pw, ph)
