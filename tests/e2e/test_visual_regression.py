"""Visual regression coverage for the primary desktop and mobile surfaces.

These screenshot contracts use the same built frontend fixture as the other
Playwright checks, but intercept every frontend API request and replace SSE
with an inert open stream. That makes the baseline independent of a local
cluster's live agents, alerts, and clock-driven event traffic.

Generate PNG references through the Visual baselines workflow, never from a
developer host or a Docker image. Dispatch the workflow on a PR head for an
intentional UI change; runner-image drift on main opens a PNG-only self-heal
PR. Both paths render on the same GitHub Ubuntu runner environment that later
compares the references. The browser context also fixes its color scheme,
locale, and timezone.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page, Route

from tests.e2e._ports import FRONTEND_URL
from tests.e2e._visual_snapshot import assert_visual_snapshot

_AGENT = {
    "agent_id": 1,
    "spawner": "user",
    "status": "idling",
    "pid": 100,
    "spawned_at": "2026-09-01T00:00:00Z",
    "started_at": "2026-09-01T00:00:00Z",
    "last_active_at": "2026-09-01T00:00:00Z",
    "label": "visual baseline agent",
    "machine": "test-host",
    "supports_vision": True,
    "notices_awaiting_response": [],
    "unread_notice_count": 0,
}

_API_STUBS: dict[str, object] = {
    "/api/agents": [_AGENT],
    "/api/auth/check": {"authenticated": True},
    "/api/settings": {"settings": []},
    "/api/notices": {"open": [], "awaiting": [], "resolved_page": [], "next_cursor": None},
    "/api/tasks": {"tasks": []},
    "/api/agents/1/timeline": {"items": [], "msg_count": 0, "has_more": False},
    "/api/agents/1/token-usage": {
        "input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "max_context_tokens": 0,
        "soft_compact_tokens": 0,
        "hard_compact_tokens": 0,
    },
    "/api/agents/1/pages": [],
    "/api/agents/1/pending": [],
    "/api/pages": [],
    "/api/fleet/graph": {"nodes": [], "edges": []},
    "/api/alerts": {"alerts": [], "meta": {"window": "24h", "total": 0, "unresolved_count": 0}},
    "/api/status": {
        "cluster": {"machines": []},
        "scheduler": {"upcoming": []},
        "services": {"services": []},
        "shells": {"shells": []},
    },
    "/api/system": {"cpu_percent": 0, "mem_percent": 0, "disk_percent": 0},
}

_INERT_EVENT_SOURCE = """
class InertEventSource {
  static CONNECTING = 0; static OPEN = 1; static CLOSED = 2;
  constructor(url) {
    this.url = url; this.readyState = InertEventSource.CONNECTING;
    this.onopen = null; this.onmessage = null; this.onerror = null;
    setTimeout(() => {
      if (this.readyState === InertEventSource.CLOSED) return;
      this.readyState = InertEventSource.OPEN;
      if (this.onopen) this.onopen(new Event("open"));
    }, 0);
  }
  close() { this.readyState = InertEventSource.CLOSED; }
  addEventListener() {}
  removeEventListener() {}
}
window.EventSource = InertEventSource;
"""


@pytest.fixture
def visual_page(frontend_proc: None, playwright_browser: Browser) -> Iterator[Page]:
    """A deterministic desktop page over the production frontend build."""
    context = playwright_browser.new_context(
        viewport={"width": 1280, "height": 800},
        color_scheme="light",
        locale="en-US",
        timezone_id="UTC",
    )
    page = context.new_page()
    page.add_init_script(_INERT_EVENT_SOURCE)

    def _stub(route: Route) -> None:
        endpoint = "/api/" + route.request.url.split("/api/", 1)[1].split("?", 1)[0]
        body = _API_STUBS.get(endpoint, {})
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    page.route("**/api/**", _stub)
    try:
        yield page
    finally:
        context.close()


def _open(page: Page, path: str) -> None:
    page.goto(f"{FRONTEND_URL}{path}", wait_until="domcontentloaded")


def test_home_visual_regression(visual_page: Page) -> None:
    """Desktop conversation shell stays visually stable."""
    _open(visual_page, "/")
    visual_page.wait_for_selector("textarea:not([disabled])", timeout=15_000)
    assert_visual_snapshot(
        visual_page,
        test_file=Path(__file__).stem,
        test_name="test_home_visual_regression",
        name="home.png",
    )


def test_fleet_visual_regression(visual_page: Page) -> None:
    """Desktop fleet shell stays visually stable."""
    _open(visual_page, "/fleet")
    visual_page.wait_for_selector("text=Fleet", timeout=15_000)
    assert_visual_snapshot(
        visual_page,
        test_file=Path(__file__).stem,
        test_name="test_fleet_visual_regression",
        name="fleet.png",
    )


def test_mobile_visual_regression(visual_page: Page) -> None:
    """The primary conversation shell stays usable at phone width."""
    visual_page.set_viewport_size({"width": 390, "height": 844})
    _open(visual_page, "/")
    visual_page.wait_for_selector("textarea:not([disabled])", timeout=15_000)
    assert_visual_snapshot(
        visual_page,
        test_file=Path(__file__).stem,
        test_name="test_mobile_visual_regression",
        name="mobile.png",
    )
