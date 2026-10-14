"""Cropped captures retain their geometry and never hide system clipping."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from base.host.permissions_helper.client import PermissionsHelperClient, ScreenSize

from .. import screen
from ..errors import ComputerUseError
from ..targets import CaptureRegion


class _Display(PermissionsHelperClient):
    """A main display at (-400,-100) 800x600; a capture writes a PNG of `png` pixels."""

    def __init__(self) -> None:
        super().__init__("/nonexistent-test-helper.sock")
        self.png: tuple[int, int] | None = (400, 200)
        self.captured: list[tuple[int, int, int, int]] = []

    def screen_size(self) -> ScreenSize:
        return {"x": -400, "y": -100, "w": 800, "h": 600, "scale": 99}

    def screencapture_region(  # pyright: ignore[reportIncompatibleMethodOverride]
        self, x: int, y: int, w: int, h: int, path: str
    ) -> None:
        if self.png is None:
            pytest.fail("out-of-display capture must not run")
        self.captured.append((x, y, w, h))
        _png(path, *self.png)


@pytest.fixture
def helper() -> _Display:
    return _Display()


def _png(path: str, width: int, height: int) -> None:
    Path(path).write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR" + struct.pack(">II", width, height)
    )


def test_capture_preserves_origin_and_measures_retina_scale(
    helper: _Display, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "capture.png"

    def capture_path(_agent_id: int) -> Path:
        return path

    monkeypatch.setattr(screen, "snapshot_path", capture_path)
    assert screen.capture_region(helper, 7, CaptureRegion(-350, -50, 200, 100)) == (
        path,
        2,
        (400, 200),
    )
    assert helper.captured == [(-350, -50, 200, 100)]


@pytest.mark.parametrize(
    "region",
    [
        CaptureRegion(-401, 0, 10, 10),
        CaptureRegion(0, -101, 10, 10),
        CaptureRegion(395, 0, 10, 10),
        CaptureRegion(0, 495, 10, 10),
    ],
)
def test_outside_main_display_fails_before_capture(helper: _Display, region: CaptureRegion) -> None:
    helper.png = None
    with pytest.raises(ComputerUseError, match="main display"):
        screen.capture_region(helper, 7, region)


def test_unexpected_clipping_is_an_error(
    helper: _Display, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def capture_path(_agent_id: int) -> Path:
        return tmp_path / "capture.png"

    monkeypatch.setattr(screen, "snapshot_path", capture_path)
    helper.png = (400, 170)
    with pytest.raises(ComputerUseError, match="dimensions"):
        screen.capture_region(helper, 7, CaptureRegion(0, 0, 200, 100))
