from __future__ import annotations

import pytest
from playwright.sync_api import Page

from tests.e2e._ports import FRONTEND_URL


@pytest.mark.scenario("tests.e2e.fakes.scenarios.lifecycle_terminate:build")
def test_first_tab_focuses_skip_link_to_main_content(
    gateway_proc: None,
    frontend_proc: None,
    playwright_page: Page,
) -> None:
    playwright_page.goto(FRONTEND_URL, wait_until="domcontentloaded")
    playwright_page.locator("#main-content").wait_for(state="attached", timeout=20_000)

    playwright_page.keyboard.press("Tab")

    skip_link = playwright_page.locator('a[href="#main-content"]')
    assert skip_link.evaluate("element => document.activeElement === element")
    box = skip_link.bounding_box()
    assert box is not None
    assert box["width"] > 1
