"""Unit contracts for the browser-backed visual snapshot helper."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from playwright.sync_api import Page

from tests.e2e._visual_snapshot import assert_visual_snapshot, snapshot_path


class _FakePage:
    def __init__(self, png: bytes, comparison: dict[str, int | bool]) -> None:
        self.png = png
        self.comparison = comparison
        self.screenshot_options: dict[str, object] | None = None

    def screenshot(self, **kwargs: object) -> bytes:
        self.screenshot_options = kwargs
        return self.png

    def evaluate(self, _expression: str, _arg: object) -> dict[str, int | bool]:
        return self.comparison


def _page(*, different_pixels: int = 0, total_pixels: int = 1_000) -> _FakePage:
    return _FakePage(
        b"actual PNG",
        {
            "sameDimensions": True,
            "differentPixels": different_pixels,
            "totalPixels": total_pixels,
            "actualWidth": 20,
            "actualHeight": 50,
            "expectedWidth": 20,
            "expectedHeight": 50,
        },
    )


def test_missing_reference_writes_the_artifact_candidate(tmp_path: Path) -> None:
    page = _page()

    with pytest.raises(pytest.fail.Exception, match="Generated visual baseline candidate"):
        assert_visual_snapshot(
            cast(Page, page),
            test_file="test_visual_regression",
            test_name="test_home_visual_regression",
            name="home.png",
            snapshots_dir=tmp_path,
        )

    candidate = snapshot_path(
        "test_visual_regression",
        "test_home_visual_regression",
        "home.png",
        snapshots_dir=tmp_path,
    )
    assert candidate == (
        tmp_path / "test_visual_regression" / "test_home_visual_regression" / "home.png"
    )
    assert candidate.read_bytes() == b"actual PNG"
    assert page.screenshot_options == {
        "animations": "disabled",
        "caret": "hide",
        "full_page": True,
    }


def test_allows_pixel_differences_at_the_configured_ratio(tmp_path: Path) -> None:
    page = _page(different_pixels=1, total_pixels=1_000)
    reference = snapshot_path(
        "test_visual_regression",
        "test_home_visual_regression",
        "home.png",
        snapshots_dir=tmp_path,
    )
    reference.parent.mkdir(parents=True)
    reference.write_bytes(b"expected PNG")

    assert_visual_snapshot(
        cast(Page, page),
        test_file="test_visual_regression",
        test_name="test_home_visual_regression",
        name="home.png",
        snapshots_dir=tmp_path,
    )


def test_rejects_pixel_differences_above_the_configured_ratio(tmp_path: Path) -> None:
    page = _page(different_pixels=2, total_pixels=1_000)
    reference = snapshot_path(
        "test_visual_regression",
        "test_home_visual_regression",
        "home.png",
        snapshots_dir=tmp_path,
    )
    reference.parent.mkdir(parents=True)
    reference.write_bytes(b"expected PNG")

    with pytest.raises(pytest.fail.Exception, match="Visual regression"):
        assert_visual_snapshot(
            cast(Page, page),
            test_file="test_visual_regression",
            test_name="test_home_visual_regression",
            name="home.png",
            snapshots_dir=tmp_path,
        )


def test_rejects_changed_screenshot_dimensions(tmp_path: Path) -> None:
    page = _FakePage(
        b"actual PNG",
        {
            "sameDimensions": False,
            "differentPixels": 0,
            "totalPixels": 1_000,
            "actualWidth": 20,
            "actualHeight": 50,
            "expectedWidth": 30,
            "expectedHeight": 50,
        },
    )
    reference = snapshot_path(
        "test_visual_regression",
        "test_home_visual_regression",
        "home.png",
        snapshots_dir=tmp_path,
    )
    reference.parent.mkdir(parents=True)
    reference.write_bytes(b"expected PNG")

    with pytest.raises(pytest.fail.Exception, match="dimensions changed"):
        assert_visual_snapshot(
            cast(Page, page),
            test_file="test_visual_regression",
            test_name="test_home_visual_regression",
            name="home.png",
            snapshots_dir=tmp_path,
        )
