"""Cropped captures retain their geometry and never hide system clipping."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from services.desktop.computer import screen
from services.desktop.computer.errors import ComputerUseError
from services.desktop.computer.targets import CaptureRegion
from services.desktop.permissions_helper.client import ScreenSize


@pytest.fixture
def main_display(monkeypatch: pytest.MonkeyPatch) -> ScreenSize:
    size: ScreenSize = {"x": -400, "y": -100, "w": 800, "h": 600, "scale": 99}
    monkeypatch.setattr(screen.helper, "screen_size", lambda: size)
    return size


def _png(path: str, width: int, height: int) -> None:
    Path(path).write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR" + struct.pack(">II", width, height)
    )


def test_capture_preserves_origin_and_measures_retina_scale(
    main_display: ScreenSize, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "capture.png"

    def capture_path(_agent_id: int) -> Path:
        return path

    monkeypatch.setattr(screen, "_snapshot_path", capture_path)
    captured: list[tuple[int, int, int, int]] = []

    def capture(x: int, y: int, w: int, h: int, output: str) -> None:
        captured.append((x, y, w, h))
        _png(output, 400, 200)

    monkeypatch.setattr(screen.helper, "screencapture_region", capture)
    assert screen.capture_region(7, CaptureRegion(-350, -50, 200, 100)) == (path, 2, (400, 200))
    assert captured == [(-350, -50, 200, 100)]


@pytest.mark.parametrize(
    "region",
    [
        CaptureRegion(-401, 0, 10, 10),
        CaptureRegion(0, -101, 10, 10),
        CaptureRegion(395, 0, 10, 10),
        CaptureRegion(0, 495, 10, 10),
    ],
)
def test_outside_main_display_fails_before_capture(
    main_display: ScreenSize, region: CaptureRegion, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden_capture(*_args: object) -> None:
        pytest.fail("out-of-display capture must not run")

    monkeypatch.setattr(screen.helper, "screencapture_region", forbidden_capture)
    with pytest.raises(ComputerUseError, match="main display"):
        screen.capture_region(7, region)


def test_unexpected_clipping_is_an_error(
    main_display: ScreenSize, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def capture_path(_agent_id: int) -> Path:
        return tmp_path / "capture.png"

    monkeypatch.setattr(screen, "_snapshot_path", capture_path)

    def clipped_capture(x: int, y: int, w: int, h: int, output: str) -> None:
        _png(output, 400, 170)

    monkeypatch.setattr(screen.helper, "screencapture_region", clipped_capture)
    with pytest.raises(ComputerUseError, match="dimensions"):
        screen.capture_region(7, CaptureRegion(0, 0, 200, 100))
