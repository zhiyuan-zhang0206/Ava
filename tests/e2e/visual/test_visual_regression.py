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
from playwright.sync_api import Browser, Locator, Page, Route, expect

from tests.e2e._ports import FRONTEND_URL
from tests.e2e.visual._visual_snapshot import assert_visual_snapshot

_AGENT = {
    "agent_id": 1,
    "spawner": "user",
    "fork_source_agent_id": None,
    "status": "idling",
    "pid": 100,
    "spawned_at": "2026-09-01T00:00:00Z",
    "started_at": "2026-09-01T00:00:00Z",
    "last_active_at": "2026-09-01T00:00:00Z",
    "last_inbound_at": "2026-09-01T00:00:00Z",
    "label": "visual baseline agent",
    "machine": "test-host",
    "supports_vision": True,
    "liveness_state": "unknown",
    "observation": {
        "machine_probe_at": None,
        "machine_probe_valid_until": None,
        "runtime_lease_expires_at": None,
        "runtime_owner": "unknown",
    },
    "heartbeat_paused_until": None,
    "awaiting_response_count": 0,
    "highest_notice_priority": None,
    "unread_notice_count": 0,
}

_API_STUBS: dict[str, object] = {
    "/api/agents": {"agents": [_AGENT], "next_cursor": None},
    "/api/agents/roster": {"agents": [_AGENT], "ancestors": []},
    "/api/agents/1": {
        **_AGENT,
        "notices_awaiting_response": [],
        "fork_source_checkpoint_id": None,
        "last_probe_at": None,
    },
    "/api/auth/check": {"authenticated": True},
    "/api/settings": {"settings": []},
    # The home shell reads the display config domain at runtime
    # (lib/display-limits.ts); an empty field list keeps the baked fallbacks.
    "/api/config": {"fields": [], "raw_overrides": {}, "machine_capabilities": []},
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


@pytest.mark.parametrize(
    "viewport,collapsed", [((1280, 480), False), ((1280, 480), True), ((390, 600), False)]
)
def test_plugin_quota_rows_wrap_inside_statistics_popover(
    visual_page: Page, viewport: tuple[int, int], collapsed: bool
) -> None:
    """Reset schedules and long details remain readable in a bounded popover."""
    page = visual_page
    width, height = viewport
    page.set_viewport_size({"width": width, "height": height})
    _stub_quota_sidebar_dependencies(page)
    label = "Codex account with a deliberately long display name"
    reset = "5h reset: 2026-09-29 06:00 UTC+08:00\nWeekly reset: 2026-10-02 06:00 UTC+08:00"
    detail = "5h remaining 60% · Weekly remaining 30% · Manual resets: 0"
    declarations = [
        {
            "plugin": f"quota-{index}",
            "id": "usage",
            "label": label if index == 0 else f"Account {index}",
        }
        for index in range(4)
    ]
    page.route(
        "**/api/ui/contributions",
        lambda route: route.fulfill(json={"stats": declarations, "nav": [], "themes": []}),
    )
    page.route(
        "**/api/stats/dashboard?*",
        lambda route: route.fulfill(
            json={
                "live_count": 1,
                "window_hours": 24,
                "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_hit_pct": 0},
                "cost_usd": 0,
                "avg_turn_seconds": None,
                "warnings": 0,
                "errors": 0,
                "alert_classes_active": 0,
                "alert_classes_dismissed": 0,
                "total_events": 0,
                "plugin_stats": [
                    {
                        **row,
                        "value": reset,
                        "detail": detail,
                        "status": "ok",
                        "updated_at": "2026-09-28T16:00:00Z",
                        "updated_by": None,
                    }
                    for row in declarations
                ],
            }
        ),
    )
    # HomeShell resets a persisted collapsed preference once settings load.
    # Wait for that write so a cold-entry effect cannot undo our later click.
    with page.expect_response(
        lambda response: (
            response.url.endswith("/api/settings/display.sidebar_collapsed")
            and response.request.method == "PUT"
        )
    ):
        _open(page, "/")
    if width < 768:
        page.get_by_role("button", name="Open sidebar", exact=True).click()
    elif collapsed:
        page.get_by_role("button", name="Collapse sidebar", exact=True).click()
        expect(page.get_by_role("button", name="Expand sidebar", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="Collapse sidebar", exact=True)).to_have_count(0)
    page.get_by_role("button", name="Statistics", exact=True).click()
    dialog = page.get_by_role("dialog").filter(has=page.get_by_text(label, exact=True))
    dialog.wait_for(state="visible")
    bounds = dialog.bounding_box()
    assert bounds is not None
    assert 0 <= bounds["x"] <= width - bounds["width"]
    assert 0 <= bounds["y"] <= height - bounds["height"]
    assert dialog.evaluate("el => el.scrollWidth <= el.clientWidth")
    assert dialog.evaluate("el => el.scrollHeight > el.clientHeight")
    _assert_quota_text_is_readable(dialog, (label, reset, detail))


def _assert_quota_text_is_readable(dialog: Locator, texts: tuple[str, ...]) -> None:
    for text in texts:
        elements = dialog.get_by_text(text, exact=True)
        assert elements.count() > 0
        for element in elements.all():
            assert element.evaluate("el => el.scrollWidth <= el.clientWidth")
            assert element.evaluate("el => getComputedStyle(el).textOverflow !== 'ellipsis'")
    last_detail = dialog.get_by_text(texts[-1], exact=True).last
    last_detail.scroll_into_view_if_needed()
    bounds = dialog.bounding_box()
    last_bounds = last_detail.bounding_box()
    assert bounds is not None and last_bounds is not None
    assert bounds["y"] <= last_bounds["y"]
    assert last_bounds["y"] + last_bounds["height"] <= bounds["y"] + bounds["height"]


def _stub_quota_sidebar_dependencies(page: Page) -> None:
    # A live sidebar reads models even while its spawn picker is closed.
    # The visual fixture's generic {} response is not a ModelsResponse.
    page.route(
        "**/api/models",
        lambda route: route.fulfill(json={"providers": {}, "models": {}, "default": ""}),
    )
    page.route("**/api/presets", lambda route: route.fulfill(json=[]))
    settings: dict[str, object] = {"display.sidebar_collapsed": True}

    def _settings(route: Route) -> None:
        if route.request.method == "PUT":
            key = route.request.url.rsplit("/", 1)[1]
            payload = route.request.post_data_json
            assert payload is not None
            settings[key] = payload["value"]
            route.fulfill(
                json={"key": key, "value": settings[key], "updated_at": "2026-09-28T16:00:00Z"}
            )
            return
        route.fulfill(
            json={
                "settings": [
                    {"key": key, "value": value, "updated_at": "2026-09-28T16:00:00Z"}
                    for key, value in settings.items()
                ]
            }
        )

    page.route("**/api/settings", _settings)
    page.route("**/api/settings/*", _settings)
