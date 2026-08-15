"""Mobile no-horizontal-overflow regression — Timeline + Fleet on a 390px viewport.

Why it exists: Task #979 (user report 2026-08-07) — the mobile timeline clipped
312px of content at the right edge with no way to scroll to it, and the fleet
Tasks toolbar pushed the page 21px wider than the viewport.

Root causes fixed in the same PR:
- `timeline-surface` (ui/web/src/app/page.tsx) was a flex item without
  `min-w-0`: its `min-width:auto` resolved to the composer footer's min-content
  (702px), so in the 390px `overflow-hidden` section it laid out 702px wide and
  the right 312px were unreachable. The vertical-axis twin (missing
  `display:flex`, Task #874) already had a class-contract test; this is the
  horizontal axis, and jsdom cannot measure layout, so it is asserted here in a
  real engine.
- The fleet Tasks toolbars (`task-graph.tsx`) were non-wrapping `shrink-0`
  rows: the `ml-auto` chips (Needs you / Done / Canceled) overflowed 390px and
  created a page-level horizontal scrollbar.

Assertions are layout invariants (no page-level horizontal scroll; the
timeline surface never wider than its parent section), so they hold on any
data state — empty timeline, real conversations, empty or full kanban.

Target environment: `AVA_MOBILE_TEST_BASE_URL` (deployed bundle, the reliable
mode) or the session-scoped `frontend_proc` build — same convention as
test_spawn_button_mobile.py.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from playwright.sync_api import Browser, BrowserContext, Page

_OVERRIDE_BASE_URL = os.environ.get("AVA_MOBILE_TEST_BASE_URL")

_IPHONE_14_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.4 "
    "Mobile/15E148 Safari/604.1"
)


@pytest.fixture(scope="module")
def mobile_chromium_context(playwright_browser: Browser) -> Iterator[BrowserContext]:
    """iPhone-like mobile context on the shared chromium browser: 390px
    viewport, touch, mobile UA. Engine-agnostic layout invariants don't need
    WebKit — chromium keeps the suite fast."""
    ctx = playwright_browser.new_context(
        user_agent=_IPHONE_14_UA,
        viewport={"width": 390, "height": 664},
        device_scale_factor=3,
        is_mobile=True,
        has_touch=True,
    )
    # Deployed clusters with a cluster secret gate the frontend behind a
    # session cookie. CI's local stack is unauthenticated; when targeting a
    # real deployment, inject a valid session via
    # `AVA_TEST_SESSION_COOKIE="ava_session=<token>"`.
    if _OVERRIDE_BASE_URL and os.environ.get("AVA_TEST_SESSION_COOKIE"):
        ctx.add_cookies(
            [
                {
                    "name": "ava_session",
                    "value": os.environ["AVA_TEST_SESSION_COOKIE"].split("=", 1)[-1],
                    "domain": _OVERRIDE_BASE_URL.split("//", 1)[-1].split(":", 1)[0],
                    "path": "/",
                }
            ]
        )
    try:
        yield ctx
    finally:
        ctx.close()


def _open(ctx: BrowserContext, base_url: str, path: str) -> Page:
    page = ctx.new_page()
    page.goto(f"{base_url}{path}", wait_until="domcontentloaded")
    # The timeline mounts SSE streams that never idle; wait for the first
    # layout-stable signal instead of networkidle.
    page.wait_for_selector('[data-testid="timeline-surface"], [role="tablist"]', timeout=15_000)
    return page


def _wait_layout_settled(page: Page) -> None:
    """The min-content that drives the mobile overflow bugs only exists once
    the composer (timeline) / tasks toolbar (fleet) has rendered — the surface
    starts at viewport width and grows when data lands. Wait for the
    layout-driving element, then give React a beat to settle."""
    page.wait_for_selector("textarea", timeout=15_000)  # composer (timeline)
    page.wait_for_timeout(1200)


def _no_page_scroll(page: Page) -> bool:
    return page.evaluate(
        "() => document.scrollingElement.scrollWidth <= document.scrollingElement.clientWidth + 1"
    )


# NOTE: the timeline page's own truncation invariant (the `min-w-0` on
# `timeline-surface`, Task #979) is guarded by a class-contract test in
# ui/web/src/app/page.test.tsx instead of a Playwright assertion: the
# timeline is SSE-fed, and a fresh Playwright context cannot open the SSE
# stream against a cookie-gated deployed cluster (EventSource stays
# CONNECTING), so the surface never grows past viewport width and the
# assertion would false-pass. The fleet page is REST-fed, which is what the
# test below exercises.


@pytest.mark.skipif(
    not _OVERRIDE_BASE_URL,
    reason="same as above — set AVA_MOBILE_TEST_BASE_URL",
)
def test_mobile_fleet_no_page_scroll(
    mobile_chromium_context: BrowserContext,
) -> None:
    """The fleet Tasks toolbar chips must not push the page wider than the
    viewport (the non-wrapping shrink-0 row regression)."""
    assert _OVERRIDE_BASE_URL is not None  # skipif gate; narrows for pyright
    page = _open(mobile_chromium_context, _OVERRIDE_BASE_URL, "/fleet")
    # The Tasks toolbar (Graph / Kanban / chips) is the overflow driver; it
    # lives in the Tasks tab. Click it, wait for the toolbar, then a beat for
    # the wrapped layout to settle.
    page.locator('[role="tab"]', has_text="Tasks").click()
    page.wait_for_selector('button:has-text("Graph")', timeout=15_000)
    page.wait_for_timeout(800)
    assert _no_page_scroll(page), "page-level horizontal scroll on /fleet (mobile)"
