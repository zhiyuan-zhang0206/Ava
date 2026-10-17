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

Suite entry points and CI integration: [[tests/docs/tests.meiva.okf.md]].
