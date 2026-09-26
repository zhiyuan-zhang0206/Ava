"""Layout invariants — real-engine layer of the two-layer defense (I1–I6).

Task #1024 (R4, layer 3): every named layout invariant (lib/layout.ts
LAYOUT_INVARIANTS) gets a Playwright assertion at the three viewport tiers
(320/390/768). The jsdom half lives in page.test.tsx + layout.test.ts.

Why fake EventSource: the timeline page is SSE-fed, and a fresh browser
context cannot open the SSE stream against a cookie-gated deployed cluster,
so without a stream the surface never grows past viewport width and every
invariant false-passes (the #979 test comment's self-admitted gap).
`page.route` cannot stream to an EventSource (evaluation finding #1), so
the suite injects a deterministic fake via `add_init_script`: open →
snapshot → delta → reconnect. The same mock stream doubles as the fold
layer's e2e scenario in the R4 layer-1 PR.

Target selection: `AVA_MOBILE_TEST_BASE_URL` set → that URL with real data
(cookie via AVA_TEST_SESSION_COOKIE); the fake stream is ALWAYS injected.
Unset → session-scoped `frontend_proc` build with stubbed /api/** — CI mode.
It also locks the shell's on-screen-keyboard contract (task #4779): with a
controllable fake `window.visualViewport` the composer must stay inside the
visible band the keyboard leaves (see the keyboard section at the end).

Engine: chromium (CI installs chromium only; webkit is reserved for the
iOS-Chrome popover test).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Locator, Page, expect

from tests.e2e._layout_assertions import (
    OVERRIDE_BASE_URL,
    all_elements_within_parents,
    element_within_parent,
    element_within_viewport,
    new_context,
    no_document_horizontal_overflow,
    open_page,
    wait_layout_settled,
)
from tests.e2e._ports import FRONTEND_URL

# The three tiers from LAYOUT_VIEWPORT_TIERS (lib/layout.ts): 320 (small
# phone) / 390 (the #979 precedent) / 768 (md breakpoint edge).
VIEWPORTS = [320, 390, 768]


@pytest.fixture(scope="session")
def frontend_target(request: pytest.FixtureRequest) -> str:
    """Base URL: override env var, else the session frontend_proc build."""
    if OVERRIDE_BASE_URL:
        return OVERRIDE_BASE_URL
    request.getfixturevalue("frontend_proc")
    return FRONTEND_URL


# ── Measurements (I1–I6) ────────────────────────────────────────────────
def _no_page_scroll(page: Page) -> bool:
    return no_document_horizontal_overflow(page)


def _surface_within_parent(page: Page) -> bool:
    return element_within_parent(page, '[data-testid="timeline-surface"]')


def _composer_within_viewport(page: Page) -> bool:
    return element_within_viewport(page, "textarea")


def _no_page_vertical_scroll(page: Page) -> bool:
    """I6: the page must not scroll as a whole — the min-h-0 flex chain
    routes scrolling into the timeline surface. True when the document has
    no vertical overflow (the surface's own scroll region carries it)."""
    return page.evaluate(
        "() => document.scrollingElement.scrollHeight <= document.scrollingElement.clientHeight + 1"
    )


def _inbox_rows_within_container(page: Page) -> bool:
    return all_elements_within_parents(page, '[data-testid="inbox-row"]')


@pytest.mark.parametrize("width", VIEWPORTS)
def test_timeline_layout_invariants(
    width: int, playwright_browser: Browser, frontend_target: str
) -> None:
    """The #874/#979 flex-contract regressions, with real content loaded."""
    ctx = new_context(playwright_browser, width)
    try:
        page = open_page(ctx, frontend_target, "/", stub_api=not OVERRIDE_BASE_URL)
        wait_layout_settled(page)
        assert _no_page_scroll(page), "I1: page-level horizontal scroll (timeline)"
        assert _surface_within_parent(page), "I2: timeline-surface wider than parent (#979)"
        assert _composer_within_viewport(page), "I3: composer overflows viewport"
        assert _no_page_vertical_scroll(page), "I6: page scrolls as a whole"
    finally:
        ctx.close()


def _assert_scrollbar_state(page: Page, track: Locator, opacity: str, state: str) -> None:
    expect(track).to_have_css("opacity", opacity, timeout=3000)
    assert _no_page_scroll(page), f"{state} scrollbar widened the document"


def _invisible_track_point(track: Locator) -> tuple[float, float]:
    box = track.bounding_box()
    assert box is not None
    x = box["x"] + box["width"] / 2
    y = box["y"] + box["height"] / 2
    assert track.evaluate(
        "(el, point) => { const hit = document.elementFromPoint(point.x, point.y); return hit === el || el.contains(hit); }",
        {"x": x, "y": y},
    ), "invisible track is not hit-testable"
    return x, y


def _assert_scrollbar_layout(viewport: Locator, content: Locator, before: dict) -> None:
    assert viewport.evaluate("el => el.clientWidth") == before["width"]
    assert content.evaluate("el => el.getBoundingClientRect().toJSON()") == before["rect"]


def _content_hover_point(viewport: Locator) -> tuple[float, float]:
    box = viewport.bounding_box()
    assert box is not None
    x = box["x"] + min(80, box["width"] / 2)
    y = box["y"] + box["height"] / 2
    assert viewport.evaluate(
        "(el, point) => el.contains(document.elementFromPoint(point.x, point.y))",
        {"x": x, "y": y},
    ), "content hover point is outside the viewport"
    return x, y


def _capture_inspector_states(page: Page, panel: Locator, prefix: str) -> None:
    """Optional full-viewport evidence with the timeline visible beside the panel."""
    evidence = Path(__file__).resolve().parents[2] / "tmp" / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    scroller = panel.locator('[data-slot="scroll-area-viewport"], .overflow-y-auto').last
    box = panel.bounding_box()
    assert box is not None

    page.mouse.move(1, 1)
    page.wait_for_timeout(1200)
    page.screenshot(path=str(evidence / f"{prefix}-rest.png"))

    page.mouse.move(box["x"] + box["width"] - 5, box["y"] + box["height"] / 2)
    page.wait_for_timeout(350)
    page.screenshot(path=str(evidence / f"{prefix}-hover.png"))

    page.mouse.move(box["x"] + 80, box["y"] + box["height"] / 2)
    before = scroller.evaluate("el => el.scrollTop")
    page.mouse.wheel(0, 400)
    page.wait_for_function(
        "([el, before]) => el.scrollTop !== before",
        arg=[scroller.element_handle(), before],
    )
    page.screenshot(path=str(evidence / f"{prefix}-scroll.png"))


def test_timeline_scrollbar_hover_reveal(playwright_browser: Browser, frontend_target: str) -> None:
    """The shared ScrollArea track stays hit-testable without taking layout space."""
    ctx = new_context(playwright_browser, 1280)
    try:
        # Repeated snapshots cause scroll events that keep resetting the idle timer.
        page = open_page(
            ctx, frontend_target, "/", stub_api=not OVERRIDE_BASE_URL, reconnect_sse=False
        )
        wait_layout_settled(page)
        assert page.evaluate("matchMedia('(hover: hover)').matches")

        surface = page.locator('[data-testid="timeline-surface"]')
        viewport = surface.locator('[data-slot="scroll-area-viewport"]')
        track = surface.locator('[data-slot="scroll-area-scrollbar"]')
        content = viewport.locator('[role="log"]')
        expect(track).to_be_attached()
        assert viewport.evaluate("el => el.scrollHeight > el.clientHeight")

        page.mouse.move(1, 1)
        _assert_scrollbar_state(page, track, "0", "at rest")
        before = viewport.evaluate(
            "el => ({width: el.clientWidth, rect: el.querySelector('[role=log]').getBoundingClientRect().toJSON()})"
        )

        track_x, track_y = _invisible_track_point(track)
        page.mouse.move(track_x, track_y)
        _assert_scrollbar_state(page, track, "1", "hovered")
        _assert_scrollbar_layout(viewport, content, before)

        page.mouse.move(1, 1)
        _assert_scrollbar_state(page, track, "0", "hidden")

        content_x, content_y = _content_hover_point(viewport)
        page.mouse.move(content_x, content_y)
        page.wait_for_timeout(350)  # Let an accidental 300 ms hover fade become observable.
        _assert_scrollbar_state(page, track, "0", "content hover")

        scroll_top = viewport.evaluate("el => el.scrollTop")
        max_scroll = viewport.evaluate("el => el.scrollHeight - el.clientHeight")
        page.mouse.wheel(0, 400 if scroll_top < max_scroll / 2 else -400)
        page.wait_for_function(
            "([el, before]) => el.scrollTop !== before",
            arg=[viewport.element_handle(), scroll_top],
        )
        page.wait_for_function(
            "el => Number(getComputedStyle(el).opacity) > 0.95",
            arg=track.element_handle(),
            timeout=2000,
        )
        assert _no_page_scroll(page), "scroll reveal widened the document"
        _assert_scrollbar_state(page, track, "0", "idle hide")
    finally:
        ctx.close()


def test_inspector_scrollbar_hover_reveal(
    playwright_browser: Browser, frontend_target: str
) -> None:
    """The inspector has the same overlay scroll surface as the timeline."""
    if OVERRIDE_BASE_URL:
        pytest.skip("the inspector scroll matrix requires deterministic stubbed sections")
    ctx = new_context(playwright_browser, 1280)
    try:
        page = open_page(
            ctx,
            frontend_target,
            "/",
            stub_api=not OVERRIDE_BASE_URL,
            reconnect_sse=False,
            open_inspector=True,
        )
        wait_layout_settled(page)
        assert page.evaluate("matchMedia('(hover: hover)').matches")
        panel = (
            page.locator("header > span")
            .get_by_text("Inspector", exact=True)
            .locator("xpath=ancestor::aside[1]")
        )
        expect(panel).to_be_visible()
        expect(page.locator('[data-testid="timeline-surface"]')).to_be_visible()
        prefix = os.environ.get("AVA_SCROLLBAR_EVIDENCE_PREFIX")
        if prefix:
            _capture_inspector_states(page, panel, prefix)

        surface = panel.locator('[data-slot="scroll-area"]')
        viewport = surface.locator('[data-slot="scroll-area-viewport"]')
        track = surface.locator('[data-slot="scroll-area-scrollbar"]')
        content = viewport.locator(":scope > div > div")
        expect(surface).to_be_attached()
        expect(track).to_be_attached()
        assert viewport.evaluate("el => el.scrollHeight > el.clientHeight")
        assert viewport.evaluate("el => el.offsetWidth === el.clientWidth"), (
            "inspector viewport reserves width for a native scrollbar"
        )
        thumb_class = track.locator('[data-slot="scroll-area-thumb"]').get_attribute("class")
        assert thumb_class is not None and "bg-border" in thumb_class

        page.mouse.move(1, 1)
        _assert_scrollbar_state(page, track, "0", "inspector at rest")
        before = {
            "width": viewport.evaluate("el => el.clientWidth"),
            "rect": content.evaluate("el => el.getBoundingClientRect().toJSON()"),
        }

        track_x, track_y = _invisible_track_point(track)
        page.mouse.move(track_x, track_y)
        _assert_scrollbar_state(page, track, "1", "inspector hovered")
        _assert_scrollbar_layout(viewport, content, before)

        page.mouse.move(1, 1)
        _assert_scrollbar_state(page, track, "0", "inspector hidden")
        content_x, content_y = _content_hover_point(viewport)
        page.mouse.move(content_x, content_y)
        page.wait_for_timeout(350)
        _assert_scrollbar_state(page, track, "0", "inspector content hover")

        scroll_top = viewport.evaluate("el => el.scrollTop")
        max_scroll = viewport.evaluate("el => el.scrollHeight - el.clientHeight")
        page.mouse.wheel(0, 400 if scroll_top < max_scroll / 2 else -400)
        page.wait_for_function(
            "([el, before]) => el.scrollTop !== before",
            arg=[viewport.element_handle(), scroll_top],
        )
        page.wait_for_function(
            "el => Number(getComputedStyle(el).opacity) > 0.95",
            arg=track.element_handle(),
            timeout=2000,
        )
        assert viewport.evaluate("el => el.clientWidth") == before["width"]
        assert _no_page_scroll(page), "inspector scroll reveal widened the document"
        _assert_scrollbar_state(page, track, "0", "inspector idle hide")
    finally:
        ctx.close()


@pytest.mark.parametrize("width", VIEWPORTS)
def test_fleet_layout_invariants(
    width: int, playwright_browser: Browser, frontend_target: str
) -> None:
    """The fleet page keeps I1 (no page scroll), I4 (Tasks toolbar never
    widens the page) and I5 (inbox rows never overflow) at every tier."""
    ctx = new_context(playwright_browser, width)
    try:
        page = open_page(ctx, frontend_target, "/fleet", stub_api=not OVERRIDE_BASE_URL)
        wait_layout_settled(page)
        assert _no_page_scroll(page), "I1: page-level horizontal scroll (fleet)"

        # Tasks toolbar — the non-wrapping shrink-0 chips row (#979 fleet half).
        # TaskGraph defaults to graph mode: "Kanban" is the mode-switch button,
        # the chips (time window / Done / Canceled) sit at the row's right end.
        page.locator('[role="tab"]', has_text="Tasks").click()
        page.wait_for_selector('button:has-text("Kanban")', timeout=15_000)
        page.wait_for_timeout(800)
        assert _no_page_scroll(page), "I4: Tasks toolbar widens the page"

        # Inbox rows — the notice queue rows must fit their container.
        page.locator('[role="tab"]', has_text="Inbox").click()
        page.wait_for_timeout(1200)
        assert _no_page_scroll(page), "I1: long-title inbox row widens the page"
        if OVERRIDE_BASE_URL:
            # Deployed target: real data; rows exist in practice but don't
            # gate on them (an empty queue is legitimate state).
            if page.locator('[data-testid="inbox-row"]').count() > 0:
                assert _inbox_rows_within_container(page), "I5: inbox row overflows"
        else:
            assert _inbox_rows_within_container(page), "I5: inbox row overflows"
    finally:
        ctx.close()


# ── On-screen keyboard (task #4779) ────────────────────────────────────
# The shell sizes itself from the LAYOUT viewport and takes its height from
# it; a phone keyboard shrinks only the VISUAL viewport. Android is covered by
# the viewport meta (`interactive-widget=resizes-content`, asserted here), iOS
# by `VisualViewportHeightSync` pinning <html> to visualViewport.height. No
# local engine shrinks only the visual viewport, so the suite installs a
# controllable fake before the app loads and leaves window.innerHeight in
# place — the iOS contract (keyboard open => visual viewport shorter, layout
# viewport untouched). The assertion that matters to users is the composer's
# bottom edge staying inside the visible band; without the fix it sits
# ~keyboard-height below it, under the keyboard.

# The iPhone tier of this suite (390x664); innerHeight (the layout viewport,
# which a phone keyboard leaves alone) is this height.
VIEWPORT_HEIGHT = 664
# A mid-keyboard visual viewport: the 184px shortfall clears the 150px
# keyboard threshold in visual-viewport-height-sync.tsx the way every phone
# keyboard does (even the smallest are ~200px).
KEYBOARD_HEIGHT = 480


# Shadows the native VisualViewport with a controllable stub: `set()` moves the
# height (and optionally the pinch scale) and dispatches the same `resize` the
# app listens for, so the shell sees a keyboard without a real one.
_FAKE_VISUAL_VIEWPORT_JS = """
(() => {
  const listeners = { resize: new Set(), scroll: new Set() };
  let state = { height: window.innerHeight, scale: 1, offsetTop: 0 };
  const viewport = {
    get height() { return state.height; },
    get width() { return window.innerWidth; },
    get scale() { return state.scale; },
    get offsetTop() { return state.offsetTop; },
    get offsetLeft() { return 0; },
    get pageTop() { return state.offsetTop; },
    get pageLeft() { return 0; },
    addEventListener(type, handler) { listeners[type]?.add(handler); },
    removeEventListener(type, handler) { listeners[type]?.delete(handler); },
  };
  const install = (target) =>
    Object.defineProperty(target, "visualViewport", { configurable: true, get: () => viewport });
  try {
    install(window);
  } catch (error) {
    install(Window.prototype);
  }
  window.__visualViewport = {
    set(height, scale = 1) {
      state = { ...state, height, scale };
      for (const handler of listeners.resize) handler(new Event("resize"));
    },
  };
})();
"""


def _set_visual_viewport(page: Page, height: int, *, scale: float = 1) -> None:
    """Move the fake visual viewport; the app's listeners run synchronously."""
    page.evaluate(
        "([height, scale]) => window.__visualViewport.set(height, scale)", [height, scale]
    )


def _shell_height_style(page: Page) -> str:
    return page.evaluate("() => document.documentElement.style.height")


def _open_keyboard_page(playwright_browser: Browser, base_url: str) -> Page:
    ctx = new_context(playwright_browser, 390)
    page = open_page(
        ctx,
        base_url,
        "/",
        stub_api=not OVERRIDE_BASE_URL,
        reconnect_sse=False,
        extra_init_script=_FAKE_VISUAL_VIEWPORT_JS,
    )
    wait_layout_settled(page)
    return page


def test_viewport_meta_asks_chrome_to_resize_content(
    playwright_browser: Browser, frontend_target: str
) -> None:
    """Android half of the contract: Chrome shrinks the layout viewport for the
    keyboard only when the viewport meta asks it to (Safari ignores the meta —
    the iOS half is the shell pin test below)."""
    page = _open_keyboard_page(playwright_browser, frontend_target)
    ctx = page.context
    try:
        content = page.locator('meta[name="viewport"]').get_attribute("content")
        assert content is not None and "interactive-widget=resizes-content" in content, content
    finally:
        ctx.close()


def test_composer_follows_the_keyboard(playwright_browser: Browser, frontend_target: str) -> None:
    """The visible band shrinks by the keyboard; composer and shell follow it."""
    page = _open_keyboard_page(playwright_browser, frontend_target)
    ctx = page.context
    try:
        _set_visual_viewport(page, KEYBOARD_HEIGHT)
        assert _shell_height_style(page) == f"{KEYBOARD_HEIGHT}px"

        box = page.locator('[data-testid="composer"]').bounding_box()
        assert box is not None
        assert box["y"] + box["height"] <= KEYBOARD_HEIGHT + 1, (
            f"composer bottom at {box['y'] + box['height']} sits below the "
            f"{KEYBOARD_HEIGHT}px visible band — under the keyboard"
        )
    finally:
        ctx.close()


def test_keyboard_edges_leave_the_shell_intact(
    playwright_browser: Browser, frontend_target: str
) -> None:
    """Chrome-sized shrinks and pinch-zoom leave the shell alone; closing the
    keyboard restores the layout height."""
    page = _open_keyboard_page(playwright_browser, frontend_target)
    ctx = page.context
    try:
        # A toolbar-sized shortfall (100px, below the 150px keyboard threshold)
        # must not pin the shell — Safari's URL bar moves by tens of pixels.
        _set_visual_viewport(page, VIEWPORT_HEIGHT - 100)
        assert _shell_height_style(page) == ""

        # Keyboard up: pinned, then the user pinch-zooms: the pin is dropped
        # rather than fighting a viewport the user is scaling.
        _set_visual_viewport(page, KEYBOARD_HEIGHT)
        assert _shell_height_style(page) == f"{KEYBOARD_HEIGHT}px"
        _set_visual_viewport(page, KEYBOARD_HEIGHT, scale=2)
        assert _shell_height_style(page) == ""

        # Back to 1x with the keyboard still up: the pin returns (the real
        # resize fires again), and closing the keyboard restores the shell.
        _set_visual_viewport(page, KEYBOARD_HEIGHT)
        assert _shell_height_style(page) == f"{KEYBOARD_HEIGHT}px"
        _set_visual_viewport(page, VIEWPORT_HEIGHT)
        assert _shell_height_style(page) == ""
    finally:
        ctx.close()


# Pinned message masking and edge continuity.
_STICKY_SURFACE = """el => {
  const style = getComputedStyle(el);
  const top = getComputedStyle(el, '::before');
  const line = getComputedStyle(el, '::after');
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = 1;
  const ctx = canvas.getContext('2d');
  const alpha = color => {
    ctx.clearRect(0, 0, 1, 1);
    ctx.fillStyle = color;
    ctx.fillRect(0, 0, 1, 1);
    return ctx.getImageData(0, 0, 1, 1).data[3];
  };
  return {
    rect: el.getBoundingClientRect().toJSON(),
    alpha: alpha(style.backgroundColor),
    topAlpha: alpha(top.backgroundColor),
    topStart: parseFloat(top.top),
    topEnd: parseFloat(top.top) + parseFloat(top.height),
    lineBottom: parseFloat(line.bottom),
    lineHeight: parseFloat(line.height),
  };
}"""


def _assert_sticky_sealed(page: Page, header_selector: str) -> dict[str, float]:
    surface = page.locator(header_selector).evaluate(_STICKY_SURFACE)
    assert surface["alpha"] == 255, "scrolling text can bleed through the header"
    assert surface["topAlpha"] == 255, "top raster seam is not masked"
    assert surface["topStart"] <= -1 and surface["topEnd"] == 0
    assert surface["lineBottom"] == 0, "separator is detached from the header edge"
    assert surface["lineHeight"] == 1
    return surface["rect"]


@pytest.mark.parametrize("width", [390, 1280])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_pinned_message_seal(
    width: int, theme: str, playwright_browser: Browser, frontend_target: str
) -> None:
    context = new_context(playwright_browser, width)
    try:
        page = open_page(
            context,
            frontend_target,
            "/",
            stub_api=not OVERRIDE_BASE_URL,
            reconnect_sse=False,
        )
        wait_layout_settled(page)
        # A transient DOM theme for CSS coverage, without changing stored preferences.
        page.evaluate(
            "theme => document.documentElement.classList.toggle('dark', theme === 'dark')", theme
        )
        row = page.locator('[data-item-id="1.2"]')
        expect(row).to_be_visible()
        header_selector = '[data-item-id="1.2"] [data-testid="card-toggle"]'
        header = page.locator(header_selector)
        if header.get_attribute("aria-expanded") == "false":
            header.click()
        row.evaluate("""el => {
          const viewport = el.closest('[data-radix-scroll-area-viewport]');
          viewport.scrollTop += el.getBoundingClientRect().top
            - viewport.getBoundingClientRect().top + 100;
        }""")
        expect(header).to_have_attribute("data-stuck", "true")
        page.mouse.move(1, 1)
        resting = _assert_sticky_sealed(page, header_selector)
        bar = page.locator('[data-testid="timeline-surface"] header').bounding_box()
        assert bar is not None
        assert abs(resting["top"] - (bar["y"] + bar["height"])) < 0.5
        header.hover()
        # Let a background-color transition reveal its final hover alpha.
        page.wait_for_timeout(200)
        assert _assert_sticky_sealed(page, header_selector) == resting, "hover changed pin geometry"
    finally:
        context.close()
