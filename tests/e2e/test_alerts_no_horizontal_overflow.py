"""Alerts section no-horizontal-scroll regression — task #1960.

User report (2026-08-29): the alerts list details (alert name / annotations)
did not wrap — the table grew past the container and the only way to read the
detail was a horizontal swipe. The fix makes the table fixed-layout, lets the
detail cell wrap (whitespace-normal + break-words, no line-clamp), and drops
the Started / Duration / Source columns below md so a phone-width viewport
keeps Severity + Alert + State.

Assertions are layout invariants (jsdom cannot measure layout), so they are
checked here in a real engine: at BOTH a 390px mobile viewport and a 1280px
desktop viewport the alerts table must not overflow its container
horizontally, and the page must not gain a horizontal scrollbar.

The section is REST-fed (GET /api/alerts), so the test stubs only that GET
with a row carrying a long alert name + a long annotation; every other
request (including the /api/alerts/stream SSE) hits the real e2e gateway.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator

import pytest
from playwright.sync_api import Browser, Page, Route

from tests.e2e._ports import FRONTEND_URL

_LONG_ALERTNAME = (
    "extremely-long-alert-name-that-must-wrap-instead-of-pushing-the-table-wider-than-the-container"
)
_LONG_SUMMARY = (
    "an annotation whose full detail must wrap across multiple lines instead "
    "of forcing the table wider than the container and demanding a horizontal "
    "scroll to read it"
)

# The stub row shape mirrors lib/types.ts AlertRow (the wire format the
# section renders from).
_STUB_ALERT = {
    "id": 1,
    "status": "unresolved",
    "severity": "error",
    "alertname": _LONG_ALERTNAME,
    "labels": {"alertname": _LONG_ALERTNAME, "severity": "error"},
    "annotations": {"summary": _LONG_SUMMARY},
    "starts_at": "2026-08-29T12:00:00Z",
    "ends_at": None,
    "fingerprint": "f-1960",
    "generator_url": "",
    "source": "health-probe",
    "notified_at": None,
    "created_at": "2026-08-29T12:00:00Z",
    "updated_at": "2026-08-29T12:00:00Z",
}

_STUB_RESPONSE = {
    "alerts": [_STUB_ALERT],
    "meta": {"window": "24h", "total": 1, "unresolved_count": 1},
}

# GET /api/alerts (+ query params) only — the /api/alerts/stream SSE goes to
# the real gateway untouched.
_ALERTS_GET_RE = re.compile(r".*/api/alerts(\?.*)?$")


@pytest.fixture
def alerts_page(
    gateway_proc: None,
    frontend_proc: None,
    playwright_browser: Browser,
) -> Iterator[Page]:
    """A page on /insights#alerts with the alerts GET stubbed. The caller
    passes its own viewport via a context built from `playwright_browser`."""
    ctx = playwright_browser.new_context(viewport={"width": 390, "height": 664})
    page = ctx.new_page()

    def _stub(route: Route) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_STUB_RESPONSE),
        )

    page.route(_ALERTS_GET_RE, _stub)
    page.goto(f"{FRONTEND_URL}/insights#alerts", wait_until="domcontentloaded")
    # The table renders once the section query resolves.
    page.wait_for_selector('[data-testid="alert-row-1"]', timeout=20_000)
    yield page
    ctx.close()


def _fits_container(page: Page) -> bool:
    """The ALERTS table container scrolls horizontally iff the alerts table
    grew past it. Scoped to the alerts section: other sections (Status) also
    render tables on the same page."""
    return page.evaluate(
        """() => {
          const container = document.querySelector(
            '[data-testid="alerts-section"] [data-slot="table-container"]',
          );
          if (!container) return true;
          return container.scrollWidth <= container.clientWidth + 1;
        }"""
    )


def _no_page_scroll(page: Page) -> bool:
    return page.evaluate(
        "() => document.scrollingElement.scrollWidth <= document.scrollingElement.clientWidth + 1"
    )


def _assert_no_horizontal_scroll(page: Page) -> None:
    assert _fits_container(page), "alerts table overflows its container horizontally"
    assert _no_page_scroll(page), "page-level horizontal scroll with the alerts section open"


@pytest.mark.scenario("tests.e2e.fakes.scenarios.lifecycle_terminate:build")
def test_alerts_table_no_horizontal_scroll_mobile(alerts_page: Page) -> None:
    # 390px = the iPhone-class viewport the mobile overflow suite uses.
    alerts_page.set_viewport_size({"width": 390, "height": 664})
    # Re-layout after the viewport change, then assert the invariant.
    alerts_page.wait_for_timeout(500)
    _assert_no_horizontal_scroll(alerts_page)


@pytest.mark.scenario("tests.e2e.fakes.scenarios.lifecycle_terminate:build")
def test_alerts_table_no_horizontal_scroll_desktop(alerts_page: Page) -> None:
    alerts_page.set_viewport_size({"width": 1280, "height": 800})
    alerts_page.wait_for_timeout(500)
    _assert_no_horizontal_scroll(alerts_page)


@pytest.mark.scenario("tests.e2e.fakes.scenarios.lifecycle_terminate:build")
def test_alerts_table_no_horizontal_scroll_tablet(alerts_page: Page) -> None:
    """768px = iPad-portrait width (QA #989): the secondary columns are hidden
    below lg so the Alert detail keeps a readable width instead of collapsing
    to ~30px between six fixed columns."""
    alerts_page.set_viewport_size({"width": 768, "height": 1024})
    alerts_page.wait_for_timeout(500)
    _assert_no_horizontal_scroll(alerts_page)
