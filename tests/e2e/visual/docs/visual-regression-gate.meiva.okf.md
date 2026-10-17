---
type: doc
title: Visual regression gate
description: How the screenshot contracts turn a page into a comparable PNG — the settle rule that keeps the capture independent of runner paint speed, and the failure evidence a drift reports.
tags:
- quality-assurance
- evaluation
---

# Visual regression gate

`test_visual_regression.py` compares three full-page PNGs (home, fleet, mobile)
against committed references. The references are minted by the Visual baselines
workflow on the same Ubuntu runner image that later compares them, so a
reference and its comparison share fonts, FreeType and the graphics stack.

The page under test is a live client render, not a static document: its
selector can match before the sidebar and the shared-context panel have
painted their first data-backed frame. A capture taken at that moment holds
loading placeholders, which is a fact about the machine's speed, not about the
UI.

```meiva-contract
id = "settled-frame-capture"
kind = "invariant"
statement = "The gate compares a settled frame: the capture is the first screenshot that is byte-identical to the next one 150 ms later, polling at most 20 times, so a slow shard compares the rendered page instead of its loading placeholders; a page that keeps changing fails at the poll deadline with its own message. The allowed difference ratio, the references and the covered surfaces are unchanged by this rule, and the pixel-difference message names the bounding box of the changed region so a real drift reports where it moved."
code = ["tests/e2e/visual/_visual_snapshot.py:settled_screenshot", "tests/e2e/visual/_visual_snapshot.py:_SETTLED_FRAME_ATTEMPTS", "tests/e2e/visual/_visual_snapshot.py:assert_visual_snapshot", "tests/e2e/visual/_visual_snapshot.py:_describe_region"]
tests = ["tests/e2e/visual/test_visual_regression.py::test_home_visual_regression", "tests/e2e/visual/test_visual_regression.py::test_fleet_visual_regression", "tests/e2e/visual/test_visual_regression.py::test_mobile_visual_regression", "tests/ui/test_visual_snapshot.py::test_reports_the_changed_region_in_the_failure_message"]
```

```meiva-contract
id = "visual-diff-sensitivity-floor"
kind = "invariant"
statement = "A difference below MAX_DIFF_PIXEL_RATIO (0.100%) is not a failure: that budget absorbs antialiasing and sub-pixel noise, so the gate reports only changes above it. On a 1280x800 surface the floor is 1024 changed pixels, which is roughly a 25-px text edit or a smaller local repaint. Measured on the home surface: renaming the sidebar's Spawn label to Spawn now changed 149 pixels (0.015%) and stayed green, while widening the sidebar column from 16% to 20% changed 15492 pixels (1.513%) and failed with changed region x=16..1246, y=11..787. A green gate therefore means no visible change above the floor, not no change at all; lowering the floor requires its own decision because the noise it absorbs is what made the gate flake."
code = ["tests/e2e/visual/_visual_snapshot.py:MAX_DIFF_PIXEL_RATIO", "tests/e2e/visual/_visual_snapshot.py:_CHANNEL_DELTA_THRESHOLD"]
tests = ["tests/e2e/visual/test_visual_regression.py::test_home_visual_regression"]
```

```meiva-contract
id = "visual-fixture-renders-the-real-ui"
kind = "invariant"
statement = "The stubs behind these surfaces answer the console's reads with bodies shaped by their backend response models, answer anything else with a 404 plus an explicit detail body and fail the test naming the endpoint, and freeze the page clock at a fixed instant so a relative label such as the sidebar's 1mo ago cannot walk across buckets on a wall clock. Each regression test also asserts that the rendered page carries no error boundary and, on the home surface, a real agent row: a fixture that answers an unknown endpoint with an empty object renders a plausible-looking error state, and a baseline captured from it hides the surface it was meant to guard."
code = ["tests/e2e/visual/test_visual_regression.py:_API_STUBS", "tests/e2e/visual/test_visual_regression.py:_FROZEN_NOW", "tests/e2e/visual/test_visual_regression.py:_assert_no_error_boundary"]
tests = ["tests/e2e/visual/test_visual_regression.py::test_home_visual_regression", "tests/e2e/visual/test_visual_regression.py::test_fleet_visual_regression", "tests/e2e/visual/test_visual_regression.py::test_mobile_visual_regression"]
```

Suite entry points and CI integration: [[tests/docs/tests.meiva.okf.md]].
