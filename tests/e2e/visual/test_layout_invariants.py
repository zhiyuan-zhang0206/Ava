from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Locator, Page, expect

from scripts.post_deploy_visual.layout_assertions import (
    all_elements_within_parents,
    element_within_parent,
    element_within_viewport,
    no_document_horizontal_overflow,
)
from tests.e2e._ports import FRONTEND_URL
from tests.e2e.visual.stubbed_home import (
    OVERRIDE_BASE_URL,
    new_context,
    open_page,
    wait_layout_settled,
)

# ava:ref tests/e2e/visual/docs/layout-invariants.ava.okf.md#layout-invariant-tiers
VIEWPORTS = [320, 390, 768]


@pytest.fixture(scope="session")
def frontend_target(request: pytest.FixtureRequest) -> str:
    if OVERRIDE_BASE_URL:
        return OVERRIDE_BASE_URL
    request.getfixturevalue("frontend_proc")
    return FRONTEND_URL


def _no_page_scroll(page: Page) -> bool:
    return no_document_horizontal_overflow(page)


def _surface_within_parent(page: Page) -> bool:
    return element_within_parent(page, '[data-testid="timeline-surface"]')


def _composer_within_viewport(page: Page) -> bool:
    return element_within_viewport(page, "textarea")


def _no_page_vertical_scroll(page: Page) -> bool:
    return page.evaluate(
        "() => document.scrollingElement.scrollHeight <= document.scrollingElement.clientHeight + 1"
    )


def _inbox_rows_within_container(page: Page) -> bool:
    return all_elements_within_parents(page, '[data-testid="inbox-row"]')


@pytest.mark.parametrize("width", VIEWPORTS)
def test_timeline_layout_invariants(
    width: int, playwright_browser: Browser, frontend_target: str
) -> None:
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
    evidence = Path(__file__).resolve().parents[3] / "tmp" / "evidence"
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


# ava:ref tests/e2e/visual/docs/layout-invariants.ava.okf.md#scroll-surface-reveal
def test_timeline_scrollbar_hover_reveal(playwright_browser: Browser, frontend_target: str) -> None:
    ctx = new_context(playwright_browser, 1280)
    try:
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
        page.wait_for_timeout(350)
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
        panel = page.locator('[data-testid="shared-inspector"]')
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
    ctx = new_context(playwright_browser, width)
    try:
        page = open_page(ctx, frontend_target, "/fleet", stub_api=not OVERRIDE_BASE_URL)
        wait_layout_settled(page)
        assert _no_page_scroll(page), "I1: page-level horizontal scroll (fleet)"

        page.locator('[role="tab"]', has_text="Tasks").click()
        page.wait_for_selector('button:has-text("Kanban")', timeout=15_000)
        page.wait_for_timeout(800)
        assert _no_page_scroll(page), "I4: Tasks toolbar widens the page"

        page.locator('[role="tab"]', has_text="Inbox").click()
        page.wait_for_timeout(1200)
        assert _no_page_scroll(page), "I1: long-title inbox row widens the page"
        if OVERRIDE_BASE_URL:
            if page.locator('[data-testid="inbox-row"]').count() > 0:
                assert _inbox_rows_within_container(page), "I5: inbox row overflows"
        else:
            assert _inbox_rows_within_container(page), "I5: inbox row overflows"
    finally:
        ctx.close()


# ava:ref tests/e2e/visual/docs/layout-invariants.ava.okf.md#on-screen-keyboard-contract
VIEWPORT_HEIGHT = 664
KEYBOARD_HEIGHT = 480


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
    page = _open_keyboard_page(playwright_browser, frontend_target)
    ctx = page.context
    try:
        content = page.locator('meta[name="viewport"]').get_attribute("content")
        assert content is not None and "interactive-widget=resizes-content" in content, content
    finally:
        ctx.close()


def test_composer_follows_the_keyboard(playwright_browser: Browser, frontend_target: str) -> None:
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
    page = _open_keyboard_page(playwright_browser, frontend_target)
    ctx = page.context
    try:
        _set_visual_viewport(page, VIEWPORT_HEIGHT - 100)
        assert _shell_height_style(page) == ""

        _set_visual_viewport(page, KEYBOARD_HEIGHT)
        assert _shell_height_style(page) == f"{KEYBOARD_HEIGHT}px"
        _set_visual_viewport(page, KEYBOARD_HEIGHT, scale=2)
        assert _shell_height_style(page) == ""

        _set_visual_viewport(page, KEYBOARD_HEIGHT)
        assert _shell_height_style(page) == f"{KEYBOARD_HEIGHT}px"
        _set_visual_viewport(page, VIEWPORT_HEIGHT)
        assert _shell_height_style(page) == ""
    finally:
        ctx.close()


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
    paneAlpha: alpha(top.backgroundColor),
    paneBlur: top.backdropFilter,
    paneZ: top.zIndex,
    paneBottom: parseFloat(top.bottom),
    topStart: parseFloat(top.top),
    lineBottom: parseFloat(line.bottom),
    lineHeight: parseFloat(line.height),
  };
}"""


_PANE_BEFORE_COLOR = "el => getComputedStyle(el, '::before').backgroundColor"
_PANE_BEFORE_TRANSITION_MS = (
    "el => parseFloat(getComputedStyle(el, '::before').transitionDuration) * 1000"
)


def _wait_for_pane_settled(page: Page, header_selector: str, *, poll_ms: int = 20) -> None:
    # ava:ref tests/e2e/visual/docs/layout-invariants.ava.okf.md#sticky-pane-settle
    locator = page.locator(header_selector)
    duration_ms = locator.evaluate(_PANE_BEFORE_TRANSITION_MS)
    page.wait_for_timeout(duration_ms)
    deadline = time.monotonic() + (duration_ms * 2 + 500) / 1000
    previous = locator.evaluate(_PANE_BEFORE_COLOR)
    while time.monotonic() < deadline:
        page.wait_for_timeout(poll_ms)
        current = locator.evaluate(_PANE_BEFORE_COLOR)
        if current == previous:
            return
        previous = current


def _assert_sticky_sealed(
    page: Page, header_selector: str, *, hovered: bool = False
) -> dict[str, float]:
    surface = page.locator(header_selector).evaluate(_STICKY_SURFACE)
    assert surface["alpha"] == 0, "button background flattens the frosted pane"
    expected_alpha = 77 if hovered else 242
    assert abs(surface["paneAlpha"] - expected_alpha) <= 1, "glass tint changed"
    assert surface["paneBlur"] == "blur(12px)", "backdrop blur is missing"
    assert surface["paneZ"] == "-10", "pane must stay behind the label"
    assert surface["topStart"] <= -1 and surface["paneBottom"] == 0
    assert surface["lineBottom"] == 0, "separator is detached from the header edge"
    assert surface["lineHeight"] == 1
    return surface["rect"]


@pytest.mark.parametrize("width", [390, 1280])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_pinned_work_detail_seal(
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
        page.evaluate(
            "theme => document.documentElement.classList.toggle('dark', theme === 'dark')", theme
        )
        turn = page.locator("[data-turn-expanded]").first
        turn_toggle = turn.locator('[data-testid="turn-toggle"]')
        if turn_toggle.get_attribute("aria-expanded") == "false":
            turn_toggle.click()
        row = page.locator('.timeline-item[data-item-id="0.0"]')
        expect(row).to_be_visible()
        header_selector = '.timeline-item[data-item-id="0.0"] [data-testid="card-toggle"]'
        header = page.locator(header_selector)
        if header.get_attribute("aria-expanded") == "false":
            header.click()
        row.locator("pre").evaluate("el => { el.style.minHeight = '1200px'; }")
        row.evaluate("""el => {
          const viewport = el.closest('[data-radix-scroll-area-viewport]');
          viewport.scrollTop += el.getBoundingClientRect().top
            - viewport.getBoundingClientRect().top + 100;
        }""")
        expect(header).to_have_attribute("data-stuck", "true")
        page.mouse.move(1, 1)
        _wait_for_pane_settled(page, header_selector)
        resting = _assert_sticky_sealed(page, header_selector)
        bar = page.locator('[data-testid="timeline-surface"] header').bounding_box()
        assert bar is not None
        turn_box = turn_toggle.bounding_box()
        assert turn_box is not None
        assert abs(turn_box["y"] - (bar["y"] + bar["height"])) < 0.5
        assert abs(resting["top"] - (turn_box["y"] + turn_box["height"])) < 0.5
        for item_id in ("1.1", "1.2"):
            primary = page.locator(
                f'.timeline-item[data-item-id="{item_id}"] [data-testid="card-toggle"]'
            )
            assert primary.evaluate("el => getComputedStyle(el).position") not in (
                "sticky",
                "fixed",
            )
        header.hover()
        _wait_for_pane_settled(page, header_selector)
        assert _assert_sticky_sealed(page, header_selector, hovered=True) == resting, (
            "hover changed pin geometry"
        )
    finally:
        context.close()
