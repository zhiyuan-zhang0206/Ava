---
type: doc
title: Visual shell layout invariants
description: The real-engine half of the shell layout defense — the Playwright suite's tiers, scroll-surface reveal, on-screen-keyboard and frosted-pane contracts over the stubbed Home page.
tags:
- evaluation
- quality-assurance
---

# Visual shell layout invariants

The real-engine half of the two-layer layout defense (task #1024 R4): every named
layout invariant in `ui/web/src/lib/layout.ts` gets a Playwright assertion at the
320 / 390 / 768 tiers; the jsdom half lives in `page.test.tsx` + `layout.test.ts`.

The suite drives the stubbed Home page (`stubbed_home.py`: a fake EventSource,
`/api/**` stubs, shared context/open/settle helpers). A fresh browser context
cannot open the real cookie-gated SSE stream and `page.route` cannot stream to an
EventSource, so an injected fake carries the snapshot → delta → reconnect
sequence; without a growing stream every invariant would false-pass.
`MEIVA_MOBILE_TEST_BASE_URL` set targets that URL with real data (cookie via
`MEIVA_TEST_SESSION_COOKIE`) and still injects the fake; unset serves the
session-scoped `frontend_proc` build with stubbed `/api/**` (CI mode). Engine:
chromium — CI installs chromium only; webkit is reserved for the iOS-Chrome
popover test.

```meiva-contract
id = "layout-invariant-tiers"
kind = "invariant"
statement = "The real-engine layer asserts the named layout invariants at the 320/390/768 tiers: I1 no page-level horizontal scroll (home and fleet), I2 the timeline surface stays inside its parent, I3 the composer stays inside the viewport, I4 the Tasks toolbar never widens the fleet page, I5 inbox rows never overflow their container, I6 the page does not scroll as a whole — the min-h-0 flex chain routes scrolling into the timeline surface."
code = ["tests/e2e/visual/test_layout_invariants.py:VIEWPORTS", "tests/e2e/visual/test_layout_invariants.py:test_timeline_layout_invariants", "tests/e2e/visual/test_layout_invariants.py:test_fleet_layout_invariants"]
tests = ["tests/e2e/visual/test_layout_invariants.py::test_timeline_layout_invariants", "tests/e2e/visual/test_layout_invariants.py::test_fleet_layout_invariants"]
```

```meiva-contract
id = "scroll-surface-reveal"
kind = "invariant"
statement = "The shared ScrollArea track stays hit-testable at opacity 0 and reveals on hover or scroll without taking layout space: the track reaches full opacity while hovered and after a wheel scroll, content hover and idle hide it back, the viewport keeps its client width (no native-scrollbar reservation), and no reveal widens the document. The inspector's secondary view carries the same overlay scroll contract as the timeline."
code = ["tests/e2e/visual/test_layout_invariants.py:test_timeline_scrollbar_hover_reveal", "tests/e2e/visual/test_layout_invariants.py:test_inspector_scrollbar_hover_reveal"]
tests = ["tests/e2e/visual/test_layout_invariants.py::test_timeline_scrollbar_hover_reveal", "tests/e2e/visual/test_layout_invariants.py::test_inspector_scrollbar_hover_reveal"]
```

```meiva-contract
id = "on-screen-keyboard-contract"
kind = "invariant"
statement = "With the on-screen keyboard up the composer stays inside the visible band: the viewport meta asks Chrome to resize content (interactive-widget=resizes-content, the Android half, which Safari ignores) and the shell pins <html> to the shrunken visualViewport height (the iOS half). A shortfall below the 150px keyboard threshold and a pinch-zoom leave the shell unpinned, and closing the keyboard restores the layout height."
code = ["tests/e2e/visual/test_layout_invariants.py:VIEWPORT_HEIGHT", "tests/e2e/visual/test_layout_invariants.py:test_viewport_meta_asks_chrome_to_resize_content", "tests/e2e/visual/test_layout_invariants.py:test_composer_follows_the_keyboard", "tests/e2e/visual/test_layout_invariants.py:test_keyboard_edges_leave_the_shell_intact"]
tests = ["tests/e2e/visual/test_layout_invariants.py::test_viewport_meta_asks_chrome_to_resize_content", "tests/e2e/visual/test_layout_invariants.py::test_composer_follows_the_keyboard", "tests/e2e/visual/test_layout_invariants.py::test_keyboard_edges_leave_the_shell_intact"]
```

```meiva-contract
id = "sticky-pane-settle"
kind = "invariant"
statement = "The frosted pinned-header pane fades between stuck and unstuck, so its ::before background is read only after one full live transition-duration plus a bounded stability poll — a mid-fade read was the #3464 CI flake — and a genuine regression still fails at the poll deadline instead of hanging. The sealed pane stays behind its label (z -10, blur 12px, tint, separator) and hover changes nothing but the tint."
code = ["tests/e2e/visual/test_layout_invariants.py:_wait_for_pane_settled", "tests/e2e/visual/test_layout_invariants.py:_assert_sticky_sealed", "tests/e2e/visual/test_layout_invariants.py:test_pinned_work_detail_seal"]
tests = ["tests/e2e/visual/test_layout_invariants.py::test_pinned_work_detail_seal"]
```

Suite entry points and CI integration: [[tests/docs/tests.meiva.okf.md]].
