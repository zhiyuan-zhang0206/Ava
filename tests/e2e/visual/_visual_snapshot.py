"""Browser-native pixel comparisons for deterministic e2e screenshot references."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import TypedDict, cast

import pytest
from playwright.sync_api import Page

SNAPSHOT_ROOT = Path(__file__).parent / "__snapshots__"
MAX_DIFF_PIXEL_RATIO = 0.001
_CHANNEL_DELTA_THRESHOLD = 16


class PixelComparison(TypedDict):
    """The browser's pixel-level comparison result."""

    sameDimensions: bool
    differentPixels: int
    totalPixels: int
    actualWidth: int
    actualHeight: int
    expectedWidth: int
    expectedHeight: int


_PIXEL_COMPARISON_SCRIPT = """
async ({ actualPng, expectedPng, pixelThreshold }) => {
  const loadImage = (png) => new Promise((resolve, reject) => {
    const image = new Image();
    image.addEventListener("load", () => resolve(image), { once: true });
    image.addEventListener(
      "error",
      () => reject(new Error("Could not decode screenshot PNG")),
      { once: true },
    );
    image.src = `data:image/png;base64,${png}`;
  });

  const [actual, expected] = await Promise.all([
    loadImage(actualPng),
    loadImage(expectedPng),
  ]);
  if (actual.width !== expected.width || actual.height !== expected.height) {
    return {
      sameDimensions: false,
      differentPixels: 0,
      totalPixels: Math.max(actual.width * actual.height, expected.width * expected.height),
      actualWidth: actual.width,
      actualHeight: actual.height,
      expectedWidth: expected.width,
      expectedHeight: expected.height,
    };
  }

  const canvas = document.createElement("canvas");
  canvas.width = actual.width;
  canvas.height = actual.height;
  const context = canvas.getContext("2d", { willReadFrequently: true });
  if (context === null) {
    throw new Error("Could not create screenshot comparison canvas");
  }

  context.drawImage(actual, 0, 0);
  const actualPixels = context.getImageData(0, 0, actual.width, actual.height).data;
  context.clearRect(0, 0, actual.width, actual.height);
  context.drawImage(expected, 0, 0);
  const expectedPixels = context.getImageData(0, 0, expected.width, expected.height).data;

  let differentPixels = 0;
  for (let index = 0; index < actualPixels.length; index += 4) {
    const delta = Math.max(
      Math.abs(actualPixels[index] - expectedPixels[index]),
      Math.abs(actualPixels[index + 1] - expectedPixels[index + 1]),
      Math.abs(actualPixels[index + 2] - expectedPixels[index + 2]),
      Math.abs(actualPixels[index + 3] - expectedPixels[index + 3]),
    );
    if (delta > pixelThreshold) {
      differentPixels += 1;
    }
  }

  return {
    sameDimensions: true,
    differentPixels,
    totalPixels: actual.width * actual.height,
    actualWidth: actual.width,
    actualHeight: actual.height,
    expectedWidth: expected.width,
    expectedHeight: expected.height,
  };
}
"""


def snapshot_path(
    test_file: str,
    test_name: str,
    name: str,
    *,
    snapshots_dir: Path = SNAPSHOT_ROOT,
) -> Path:
    """Return the CI artifact path shared by candidate creation and comparison."""
    return snapshots_dir / test_file / test_name / name


def _compare_pngs(page: Page, actual_png: bytes, expected_png: bytes) -> PixelComparison:
    comparison = page.evaluate(
        _PIXEL_COMPARISON_SCRIPT,
        {
            "actualPng": base64.b64encode(actual_png).decode("ascii"),
            "expectedPng": base64.b64encode(expected_png).decode("ascii"),
            "pixelThreshold": _CHANNEL_DELTA_THRESHOLD,
        },
    )
    return cast(PixelComparison, comparison)


def assert_visual_snapshot(
    page: Page,
    *,
    test_file: str,
    test_name: str,
    name: str,
    snapshots_dir: Path = SNAPSHOT_ROOT,
) -> None:
    """Fail on missing or materially changed visual references, saving candidates."""
    actual_png = page.screenshot(animations="disabled", caret="hide", full_page=True)
    reference = snapshot_path(test_file, test_name, name, snapshots_dir=snapshots_dir)
    if not reference.exists():
        reference.parent.mkdir(parents=True, exist_ok=True)
        reference.write_bytes(actual_png)
        pytest.fail(
            f"Generated visual baseline candidate at {reference}. "
            "Review and commit it before rerunning the test."
        )

    comparison = _compare_pngs(page, actual_png, reference.read_bytes())
    if not comparison["sameDimensions"]:
        pytest.fail(
            "Visual regression: screenshot dimensions changed from "
            f"{comparison['expectedWidth']}x{comparison['expectedHeight']} to "
            f"{comparison['actualWidth']}x{comparison['actualHeight']}."
        )

    total_pixels = comparison["totalPixels"]
    if total_pixels == 0:
        pytest.fail("Visual regression: screenshot has no pixels to compare.")

    changed_pixels = comparison["differentPixels"]
    diff_ratio = changed_pixels / total_pixels
    if diff_ratio > MAX_DIFF_PIXEL_RATIO:
        pytest.fail(
            "Visual regression: "
            f"{changed_pixels}/{total_pixels} pixels changed ({diff_ratio:.3%}); "
            f"allowed ratio is {MAX_DIFF_PIXEL_RATIO:.3%}."
        )
