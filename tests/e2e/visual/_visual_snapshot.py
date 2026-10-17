from __future__ import annotations

import base64
from pathlib import Path
from typing import TypedDict, cast

import pytest
from playwright.sync_api import Page

SNAPSHOT_ROOT = Path(__file__).parent / "__snapshots__"
# meiva:ref tests/e2e/visual/docs/visual-regression-gate.meiva.okf.md#visual-diff-sensitivity-floor
MAX_DIFF_PIXEL_RATIO = 0.001
_CHANNEL_DELTA_THRESHOLD = 16
# meiva:ref tests/e2e/visual/docs/visual-regression-gate.meiva.okf.md#settled-frame-capture
_SETTLED_FRAME_ATTEMPTS = 20
_SETTLED_FRAME_INTERVAL_MS = 150


class PixelRegion(TypedDict):
    x: int
    y: int
    width: int
    height: int


class PixelComparison(TypedDict):
    sameDimensions: bool
    differentPixels: int
    totalPixels: int
    actualWidth: int
    actualHeight: int
    expectedWidth: int
    expectedHeight: int
    changedRegion: PixelRegion | None


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
      changedRegion: null,
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
  let minX = actual.width;
  let minY = actual.height;
  let maxX = -1;
  let maxY = -1;
  for (let index = 0; index < actualPixels.length; index += 4) {
    const delta = Math.max(
      Math.abs(actualPixels[index] - expectedPixels[index]),
      Math.abs(actualPixels[index + 1] - expectedPixels[index + 1]),
      Math.abs(actualPixels[index + 2] - expectedPixels[index + 2]),
      Math.abs(actualPixels[index + 3] - expectedPixels[index + 3]),
    );
    if (delta > pixelThreshold) {
      differentPixels += 1;
      const pixel = index / 4;
      const x = pixel % actual.width;
      const y = (pixel - x) / actual.width;
      if (x < minX) minX = x;
      if (x > maxX) maxX = x;
      if (y < minY) minY = y;
      if (y > maxY) maxY = y;
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
    changedRegion:
      differentPixels === 0
        ? null
        : { x: minX, y: minY, width: maxX - minX + 1, height: maxY - minY + 1 },
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
    return snapshots_dir / test_file / test_name / name


# meiva:ref tests/e2e/visual/docs/visual-regression-gate.meiva.okf.md#settled-frame-capture
def settled_screenshot(page: Page) -> bytes:
    def capture() -> bytes:
        return page.screenshot(animations="disabled", caret="hide", full_page=True)

    previous = capture()
    for _ in range(_SETTLED_FRAME_ATTEMPTS):
        page.wait_for_timeout(_SETTLED_FRAME_INTERVAL_MS)
        current = capture()
        if current == previous:
            return current
        previous = current
    pytest.fail(
        "Visual regression: the page kept changing for "
        f"{_SETTLED_FRAME_ATTEMPTS * _SETTLED_FRAME_INTERVAL_MS} ms, so no settled frame "
        "could be compared (an animation or a repeating render never stops)."
    )


def _describe_region(region: PixelRegion | None) -> str:
    if region is None:
        return "none"
    return (
        f"x={region['x']}..{region['x'] + region['width'] - 1}, "
        f"y={region['y']}..{region['y'] + region['height'] - 1}"
    )


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
    actual_png = settled_screenshot(page)
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
            f"allowed ratio is {MAX_DIFF_PIXEL_RATIO:.3%}; "
            f"changed region is {_describe_region(comparison['changedRegion'])}."
        )
