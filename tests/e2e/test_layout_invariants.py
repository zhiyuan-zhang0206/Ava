"""Layout invariants — real-engine layer of the two-layer defense (I1–I6).

Task #1024 (R4, layer 3): every named layout invariant (lib/layout.ts
LAYOUT_INVARIANTS) gets a Playwright assertion at the three viewport tiers
(320/390/768). The jsdom half lives in page.test.tsx + layout.test.ts.

Why fake EventSource: the timeline page is SSE-fed, and a fresh browser
context cannot open the SSE stream against a cookie-gated deployed cluster,
so without a stream the surface never grows past viewport width and every
invariant false-passes (the #979 test comment's self-admitted gap).
`page.route` cannot stream to an EventSource (evaluation finding #1), so
this test injects a deterministic fake via `add_init_script`: open →
snapshot → delta → reconnect. The same mock stream doubles as the fold
layer's e2e scenario in the R4 layer-1 PR.

Target selection: `AVA_MOBILE_TEST_BASE_URL` set → that URL with real data
(cookie via AVA_TEST_SESSION_COOKIE); the fake stream is ALWAYS injected.
Unset → session-scoped `frontend_proc` build with stubbed /api/** — CI mode.
Engine: chromium (CI installs chromium only; webkit is reserved for the
iOS-Chrome popover test).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from playwright.sync_api import Browser, BrowserContext, Locator, Page, Route, expect

from tests.e2e._layout_assertions import (
    all_elements_within_parents,
    element_within_parent,
    element_within_viewport,
    no_document_horizontal_overflow,
)
from tests.e2e._ports import FRONTEND_URL

_OVERRIDE_BASE_URL = os.environ.get("AVA_MOBILE_TEST_BASE_URL")

# The three tiers from LAYOUT_VIEWPORT_TIERS (lib/layout.ts): 320 (small
# phone) / 390 (the #979 precedent) / 768 (md breakpoint edge).
VIEWPORTS = [320, 390, 768]

_AGENT = {
    "agent_id": 1,
    "spawner": "user",
    "fork_source_agent_id": None,
    "status": "idling",
    "pid": 100,
    "spawned_at": "2026-08-07T00:00:00Z",
    "started_at": "2026-08-07T00:00:00Z",
    "last_active_at": "2026-08-07T00:00:00Z",
    "last_inbound_at": "2026-08-07T00:00:00Z",
    "label": "layout-test",
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
    "awaiting_response_count": 1,
    "highest_notice_priority": "P2",
    "unread_notice_count": 1,
}

_AWAITING_NOTICES = [
    {
        "id": 1,
        "agent_id": 1,
        "agent_label": "layout-test",
        "title": "Needs a decision",
        "content": "Pick A or B.",
        "priority": "P2",
        "require_response": True,
        "blocking": False,
        "created_at": "2026-08-07T00:00:00Z",
        "updated_at": "2026-08-07T00:00:00Z",
    }
]

_FYI_NOTICE = {
    "id": 2,
    "agent_id": 1,
    "agent_label": "layout-test",
    "title": "\u8f7b\u63d0\u9192\uff1a" + "x" * 40,
    "content": "Just so you know.",
    "priority": "P3",
    "require_response": False,
    "blocking": False,
    "created_at": "2026-08-07T00:00:00Z",
    "updated_at": "2026-08-07T00:00:00Z",
}

_RESOLVED_NOTICE = {
    "id": 3,
    "agent_id": 1,
    "agent_label": "layout-test",
    "title": "Done deal",
    "content": "Resolved earlier.",
    "priority": "P2",
    "require_response": True,
    "blocking": False,
    "created_at": "2026-08-06T00:00:00Z",
    "updated_at": "2026-08-06T00:00:00Z",
    "resolved_at": "2026-08-06T01:00:00Z",
    "resolution": "answered",
    "reply": "ok",
}

_TASKS = {
    "tasks": [
        {
            "id": 100 + i,
            "parent_id": None if i == 0 else 100,
            "title": t,
            "description": "",
            "results": None,
            "status": s,
            "priority": pr,
            "owner": None if i == 0 else 1,
            "owner_label": None if i == 0 else "layout-test",
            "created_by": "e2e",
            "created_at": "2026-08-07T00:00:00Z",
            "updated_at": "2026-08-07T00:00:00Z",
            "reminder_count": 0,
        }
        for i, (t, s, pr) in enumerate(
            [
                ("Root", "in_progress", "P2"),
                ("In-progress subtask", "in_progress", "P1"),
                ("Done subtask", "done", "P3"),
            ]
        )
    ]
}

# Endpoint → stub body. Layout invariants hold on any data state; the fleet
# Inbox/Tasks tabs get real rows so their layout-driving UI renders.
_API_STUBS: dict[str, object] = {
    "/api/agents": {"agents": [_AGENT], "next_cursor": None},
    "/api/agents/roster": {"agents": [_AGENT], "ancestors": []},
    "/api/agents/1": {
        **_AGENT,
        "notices_awaiting_response": _AWAITING_NOTICES,
        "fork_source_checkpoint_id": None,
        "last_probe_at": None,
    },
    "/api/notices": {
        "open": [_FYI_NOTICE],
        "awaiting": _AWAITING_NOTICES,
        "resolved_page": [_RESOLVED_NOTICE],
        "next_cursor": None,
    },
    "/api/tasks": _TASKS,
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
    "/api/system": {"cpu_percent": 0, "mem_percent": 0, "disk_percent": 0},
    "/api/auth/check": {"authenticated": True},
    "/api/status": {
        "cluster": {"machines": []},
        "scheduler": {"upcoming": []},
        "services": {"services": []},
        "shells": {"shells": []},
    },
    "/api/settings": {"settings": []},
    "/api/agents/1/inspect/live": {
        "agent_id": 1,
        "machine": "test-host",
        "status": "running",
        "liveness_state": "online",
        "last_probe_at": None,
        "shells_available": True,
        "shells": [{"id": 1, "name": "dev-server", "created_at": None, "uptime_seconds": 120}],
        "config_overlay": {f"setting_{i}": f"value_{i}" for i in range(24)},
        "preset_name": None,
        "heartbeat": {
            "interval_s": 300,
            "next_at": None,
            "paused_until": None,
            "heartbeat_pending": False,
            "last_pause": None,
        },
        "notice": None,
        "spawned_at": "2026-09-17T00:00:00Z",
        "started_at": "2026-09-17T00:00:05Z",
    },
    "/api/agents/1/inspect/statistics": {
        "agent_id": 1,
        "window_hours": 24,
        "metadata": {
            "collection": "observed",
            "window_start": None,
            "window_end": "2026-09-17T00:00:00Z",  # time-bomb-ok: historical metadata; no clock-dependent assertion
            "sampled_at": "2026-09-17T00:00:00Z",
            "collection_started_at": "2026-09-01T00:00:00Z",
            "last_observed_at": "2026-09-17T00:00:00Z",
            "cost": {"availability": "observed", "sources": ["observations"]},
            "turns": {
                "availability": "observed",
                "sources": ["observations"],
                "duration_precision": "exact",
            },
            "activity": {"availability": "observed", "sources": ["observations"]},
            "lifecycle": {"availability": "observed", "sources": ["state_transitions"]},
        },
        "cost": {
            "cost_usd": 0.42,
            "unpriced_calls": 1,
            "llm_calls": 142,
            "tokens_in": 1200000,
            "tokens_out": 84000,
            "tokens_cached": 1100000,
            "tokens_reasoning": 5000,
            "cache_hit_pct": 91.7,
        },
        "stats": {
            "turn_total": 7,
            "turn_ok": 6,
            "turn_p50_seconds": 3.1,
            "turn_p90_seconds": 9.4,
            "turn_min_seconds": 1.2,
            "turn_max_seconds": 41,
            "exec_ok": 51,
            "exec_failed": 2,
        },
        "tps": {"lm_stage_tps": 42.5, "agent_lifecycle_tps": 8.3},
        "activity": {
            "active_seconds": 1800,
            "alive_seconds": 3600,
            "active_rate": 0.5,
            "llm_seconds": 1200,
            "exec_seconds": 450,
        },
    },
    "/api/agents/1/inspect/widgets": [],
    "/api/agents/1/pending": [],
    "/api/pages": [],
    "/api/fleet/graph": {"nodes": [], "edges": []},
}


def _timeline_items() -> list[dict]:
    """A prompt, a user message, a long reply, and a run of follow-ups so
    the surface is tall enough to scroll internally (I6 needs real mass)."""
    items = [
        {
            "item_id": "0.0",
            "kind": "system_prompt",
            "payload": "You are a test agent.",
            "show_timestamp": False,
        },
        {
            "item_id": "1.1",
            "kind": "inbound_chat",
            "payload": "hello — layout invariants should hold on any data",
            "show_timestamp": True,
            "source": "user",
            "created_at": "2026-08-07T00:00:00Z",
            "inbound_id": 1,
        },
        {
            "item_id": "1.2",
            "kind": "agent_chat",
            "payload": (
                "A long reply that keeps the composer's min-content as the "
                "widest thing in the surface. " + "word " * 400
            ),
            "show_timestamp": True,
            "source": "agent",
            "created_at": "2026-08-07T00:00:01Z",
        },
    ]
    for i in range(3, 33):
        items.append(
            {
                "item_id": f"1.{i}",
                "kind": "agent_chat",
                "payload": f"Follow-up message {i} — body text for vertical mass.",
                "show_timestamp": True,
                "source": "agent",
                "created_at": f"2026-08-07T00:00:{i:02d}Z",
            }
        )
    return items


_FAKE_SSE_JS = """
(() => {
  const SNAPSHOT = __SNAPSHOT_JSON__;
  const RECONNECT = __RECONNECT__;
  const DELTA = {"role": "chat_delta", "agent_id": 1, "item_id": "1.33",
    "content": "streaming tail"};
  const HB = {"role": "heartbeat"};
  const push = (es, ev, delay) => setTimeout(() => {
    if (es.readyState === 2) return; // closed
    if (es.onmessage) es.onmessage({ data: JSON.stringify(ev) });
  }, delay);
  class FakeEventSource {
    static CONNECTING = 0; static OPEN = 1; static CLOSED = 2;
    constructor(url, opts) {
      this.url = url; this.readyState = FakeEventSource.CONNECTING;
      this.onopen = null; this.onmessage = null; this.onerror = null;
      this._timers = [];
      const all = url.includes("/api/system/all");
      const self = this;
      this._timers.push(setTimeout(() => {
        if (self.readyState === 2) return;
        self.readyState = FakeEventSource.OPEN;
        if (self.onopen) self.onopen({});
        if (all) {
          push(self, SNAPSHOT, 80);
          push(self, DELTA, 250);
          // When enabled, close the stream so a fresh instance re-runs
          // the whole script (the frontend's backoff reopen does `new
          // EventSource(...)` again).
          if (RECONNECT) {
            this._timers.push(setTimeout(() => {
              self.readyState = FakeEventSource.CLOSED;
              if (self.onerror) self.onerror({});
            }, 900));
          }
        } else {
          push(self, HB, 5000);
        }
      }, 30));
    }
    close() { this.readyState = FakeEventSource.CLOSED; }
    addEventListener() {}
    removeEventListener() {}
  }
  window.EventSource = FakeEventSource;
})();
""".replace(
    "__SNAPSHOT_JSON__",
    json.dumps(
        {"role": "timeline_snapshot", "agent_id": 1, "msg_count": 33, "items": _timeline_items()}
    ),
)


@pytest.fixture(scope="session")
def _frontend_target(request: pytest.FixtureRequest) -> str:
    """Base URL: override env var, else the session frontend_proc build."""
    if _OVERRIDE_BASE_URL:
        return _OVERRIDE_BASE_URL
    request.getfixturevalue("frontend_proc")
    return FRONTEND_URL


def _context(browser: Browser, width: int) -> BrowserContext:
    ctx = browser.new_context(
        viewport={"width": width, "height": 664},
        device_scale_factor=2 if width < 768 else 1,
    )
    if _OVERRIDE_BASE_URL and os.environ.get("AVA_TEST_SESSION_COOKIE"):
        cookie = os.environ["AVA_TEST_SESSION_COOKIE"].split("=", 1)[-1]
        host = _OVERRIDE_BASE_URL.split("//", 1)[-1].split(":", 1)[0]
        ctx.add_cookies([{"name": "ava_session", "value": cookie, "domain": host, "path": "/"}])
    return ctx


def _open(
    ctx: BrowserContext,
    base_url: str,
    path: str,
    *,
    stub_api: bool,
    reconnect_sse: bool = True,
    open_inspector: bool = False,
) -> Page:
    page = ctx.new_page()
    # The fake EventSource is ALWAYS injected (timeline invariants need the
    # stream even against a deployed bundle; the fleet page ignores it).
    page.add_init_script(_FAKE_SSE_JS.replace("__RECONNECT__", json.dumps(reconnect_sse)))
    if stub_api:

        def _stub(route: Route) -> None:
            url = route.request.url
            endpoint = "/api/" + url.split("/api/", 1)[1].split("?", 1)[0]
            body = (
                {"settings": [{"key": "display.inspector_open", "value": True}]}
                if open_inspector and endpoint == "/api/settings"
                else _API_STUBS.get(endpoint)
            )
            if body is not None:
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(body),
                )
            else:
                route.fulfill(status=404, body='{"detail": "stub: unknown endpoint"}')

        page.route("**/api/**", _stub)
    page.goto(f"{base_url}{path}", wait_until="domcontentloaded")
    return page


def _wait_layout_settled(page: Page) -> None:
    """Wait for the layout-driving element, then a beat for React + SSE."""
    page.wait_for_selector("textarea, [role='tablist']", timeout=15_000)
    page.wait_for_timeout(1500)


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
    width: int, playwright_browser: Browser, _frontend_target: str
) -> None:
    """The #874/#979 flex-contract regressions, with real content loaded."""
    ctx = _context(playwright_browser, width)
    try:
        page = _open(ctx, _frontend_target, "/", stub_api=not _OVERRIDE_BASE_URL)
        _wait_layout_settled(page)
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


def test_timeline_scrollbar_hover_reveal(
    playwright_browser: Browser, _frontend_target: str
) -> None:
    """The shared ScrollArea track stays hit-testable without taking layout space."""
    ctx = _context(playwright_browser, 1280)
    try:
        # Repeated snapshots cause scroll events that keep resetting the idle timer.
        page = _open(
            ctx, _frontend_target, "/", stub_api=not _OVERRIDE_BASE_URL, reconnect_sse=False
        )
        _wait_layout_settled(page)
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
    playwright_browser: Browser, _frontend_target: str
) -> None:
    """The inspector has the same overlay scroll surface as the timeline."""
    if _OVERRIDE_BASE_URL:
        pytest.skip("the inspector scroll matrix requires deterministic stubbed sections")
    ctx = _context(playwright_browser, 1280)
    try:
        page = _open(
            ctx,
            _frontend_target,
            "/",
            stub_api=not _OVERRIDE_BASE_URL,
            reconnect_sse=False,
            open_inspector=True,
        )
        _wait_layout_settled(page)
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
    width: int, playwright_browser: Browser, _frontend_target: str
) -> None:
    """The fleet page keeps I1 (no page scroll), I4 (Tasks toolbar never
    widens the page) and I5 (inbox rows never overflow) at every tier."""
    ctx = _context(playwright_browser, width)
    try:
        page = _open(ctx, _frontend_target, "/fleet", stub_api=not _OVERRIDE_BASE_URL)
        _wait_layout_settled(page)
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
        if _OVERRIDE_BASE_URL:
            # Deployed target: real data; rows exist in practice but don't
            # gate on them (an empty queue is legitimate state).
            if page.locator('[data-testid="inbox-row"]').count() > 0:
                assert _inbox_rows_within_container(page), "I5: inbox row overflows"
        else:
            assert _inbox_rows_within_container(page), "I5: inbox row overflows"
    finally:
        ctx.close()
