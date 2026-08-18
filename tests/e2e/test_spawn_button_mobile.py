"""Mobile (touch) SpawnButton popover smoke — local repro of the iOS Chrome bug.

Why it exists: iOS Chrome is forced to WebKit by Apple's policy, so to
reproduce iOS-Chrome-specific touch / popover bugs locally the right
engine is Playwright `webkit` (not `chromium`). The chromium+iPhone
descriptor catches a different class of mobile bugs (touch handler
wiring, viewport math) but is consistently slower under headless WAN
loading and started flaking against the deployed bundle, so this test
is webkit-only; the chromium fixture in `tests/e2e/conftest.py` is left
in place for the desktop suite to keep using it.

The test taps the Spawn button on an iPhone-emulated WebKit context and
asserts the popover dropdown opens.

Target environment is selected by the `AVA_MOBILE_TEST_BASE_URL` env var.
When set, the test hits that URL directly (useful for local repro against
the deployed prod URL).
When unset, the test falls back to the session-scoped `frontend_proc` build —
note that the `frontend_proc` path currently re-mounts the layout under
stubbed-route conditions, so the in-CI mode is being tracked as a
follow-up; the deployed-target mode is the one that exercises the actual
shipped bundle.

Regression target: PR #492 (Popover modal=true) + PR #493 (@base-ui/react
1.4.1 → 1.5.0). Before those, `tap` on iOS Chrome silently failed to open
the popover; the assertion below would time out.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator

import pytest
from playwright.sync_api import Browser, BrowserContext, Page, Playwright, Route

from tests.e2e._ports import FRONTEND_URL

# Override target via env: `AVA_MOBILE_TEST_BASE_URL=https://ava.example.com`
# runs the matrix against the deployed bundle. Unset → fall back to the
# session-scoped frontend_proc build.
_OVERRIDE_BASE_URL = os.environ.get("AVA_MOBILE_TEST_BASE_URL")


# Minimal SystemStatus body — frontend SpawnButton only reads
# `cluster.machines`; the rest of `/api/status` (scheduler / services / shells)
# is not consulted by the picker so plausible empty defaults are enough.
_STUB_STATUS_BODY = {
    "cluster": {
        "machines": [
            {
                "name": "wsl",
                "role": "agent-runner",
                "online": True,
                "paused": False,
            },
            {
                "name": "test-host",
                "role": "agent-runner",
                "online": True,
                "paused": False,
            },
        ],
    },
    "scheduler": {"upcoming": []},
    "services": {"services": []},
    "shells": {"shells": []},
}

# Endpoint → stub body. Keep them just-shaped-enough so the frontend doesn't
# enter an error state that remounts the layout (the hamburger we tap into
# lives in a re-rendered subtree).
_API_STUBS: dict[str, object] = {
    "/api/status": _STUB_STATUS_BODY,
    "/api/agents": [],
    "/api/stats/dashboard": {
        "live_count": 0,
        "window_hours": 24,
        "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_hit_pct": 0},
        "cost_usd": 0,
        "avg_turn_seconds": None,
        "warnings": 0,
        "errors": 0,
        "total_events": 0,
    },
    "/api/system": {"cpu_percent": 0, "mem_percent": 0, "disk_percent": 0},
}


@pytest.fixture(scope="session")
def webkit_browser(playwright_runtime: Playwright) -> Iterator[Browser]:
    """Playwright `webkit` browser — closest local match to iOS Chrome's
    runtime (iOS forces every browser to WebKit). Reuses the session-shared
    `playwright_runtime` because sync_playwright rejects nested entries.
    """
    browser = playwright_runtime.webkit.launch(headless=True)
    try:
        yield browser
    finally:
        browser.close()


# Inlined from `playwright.devices['iPhone 14']` (Playwright 1.x). We don't
# call `sync_playwright()` to fetch it because doing so inside an already-
# running sync session raises "using Sync API inside the asyncio loop".
_IPHONE_14_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.4 "
    "Mobile/15E148 Safari/604.1"
)


def _iphone_context(browser: Browser) -> BrowserContext:
    """Build a browser context with iPhone 14 emulation: mobile viewport,
    `has_touch`, `is_mobile`, iOS user agent. Works on either webkit or
    chromium — the underlying engine differs but the emulated input model
    matches.

    The gateway is unauthenticated (private-network-only), so no auth header is needed
    whether the target is the deployed bundle or the `frontend_proc` build.
    """
    return browser.new_context(
        user_agent=_IPHONE_14_UA,
        viewport={"width": 390, "height": 664},
        device_scale_factor=3,
        is_mobile=True,
        has_touch=True,
    )


def _open_with_status_stub(ctx: BrowserContext, base_url: str) -> Page:
    """Open the picker entry point.

    When hitting `frontend_proc` (no real backend behind it) we stub the
    `/api/*` set so the SPA boots cleanly. When hitting deployed prod we
    let real network through — there are ≥2 agent-runners online there in
    practice, which is exactly the state the picker needs to render.
    """
    page = ctx.new_page()

    if base_url == FRONTEND_URL:

        def _stub(route: Route) -> None:
            path = route.request.url.split("?")[0].rsplit("/api/", 1)
            endpoint = "/api/" + path[1] if len(path) == 2 else "/api/_unknown"
            body = _API_STUBS.get(endpoint)
            if body is not None:
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(body),
                )
            else:
                route.fulfill(status=204)

        page.route("**/api/**", _stub)

    page.goto(base_url, wait_until="domcontentloaded")
    # Page mounts SSE streams that never idle, so we can't wait for
    # networkidle. Instead wait for the hamburger to be stable before
    # interacting — the first paint takes a few hundred ms of Query/Suspense
    # churn during which the button re-mounts.
    hamburger = page.locator('[aria-label="Open sidebar"]')
    hamburger.wait_for(state="visible", timeout=10_000)
    # On mobile viewports (<md) the sidebar is collapsed behind a hamburger.
    # Open it before we can interact with the Spawn button inside the drawer.
    hamburger.click()
    # Wait until the picker SpawnButton inside the mobile drawer becomes
    # visible. There's also a copy in the collapsed-desktop sidebar slot
    # that stays hidden on mobile viewports — the `:visible` filter picks
    # only the drawer one. Long-ish timeout because mobile chromium first
    # paint over the WAN occasionally creeps past 10 s.
    page.wait_for_selector(
        '[aria-label="Spawn Agent"][aria-haspopup="dialog"]:visible',
        timeout=20_000,
    )
    return page


@pytest.mark.skipif(
    not _OVERRIDE_BASE_URL,
    reason=(
        "Set AVA_MOBILE_TEST_BASE_URL to a reachable frontend (e.g. the "
        "deployed prod URL) to run. The default CI image does not ship the "
        "Playwright webkit binary, and the frontend_proc fallback is flaky "
        "under stubbed routes — track as follow-up."
    ),
)
def test_spawn_popover_opens_on_mobile_tap(
    request: pytest.FixtureRequest,
    webkit_browser: Browser,
) -> None:
    """Tap the Spawn button on an iPhone-emulated WebKit context and assert
    the 'Spawn on' popover header appears within 2 s.

    Target URL: `AVA_MOBILE_TEST_BASE_URL` env var takes priority. When
    unset, the test requests the `frontend_proc` fixture to build & serve
    the local bundle (this path is currently flaky under stubbed routes —
    track as a follow-up).
    """
    if _OVERRIDE_BASE_URL:
        base_url = _OVERRIDE_BASE_URL
    else:
        # Only spin up the local build when the override is unset, so the
        # cheap "point at deployed" path doesn't pay for a frontend build.
        request.getfixturevalue("frontend_proc")
        base_url = FRONTEND_URL

    ctx = _iphone_context(webkit_browser)
    try:
        page = _open_with_status_stub(ctx, base_url)
        # Two SpawnButton copies render: the collapsed-desktop-sidebar slot
        # (display:none on mobile) and the mobile drawer button (visible).
        # Pick the visible one — Playwright's `:visible` engine filter.
        spawn_button = page.locator('[aria-label="Spawn Agent"][aria-haspopup="dialog"]:visible')
        # Sanity: there are >=2 agent-runners, so the SpawnButton is in
        # popover-trigger mode (not single-host direct-click mode).
        assert spawn_button.count() == 1, (
            f"expected exactly one visible picker, got {spawn_button.count()}"
        )

        spawn_button.tap()

        popover_header = page.get_by_text("Spawn on", exact=True)
        popover_header.wait_for(state="visible", timeout=2000)
    finally:
        ctx.close()
