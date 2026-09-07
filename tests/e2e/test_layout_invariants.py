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

import pytest
from playwright.sync_api import Browser, BrowserContext, Page, Route

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
    "status": "idling",
    "pid": 100,
    "spawned_at": "2026-08-07T00:00:00Z",
    "started_at": "2026-08-07T00:00:00Z",
    "last_active_at": "2026-08-07T00:00:00Z",
    "label": "layout-test",
    "machine": "test-host",
    "supports_vision": True,
    "notices_awaiting_response": [
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
    ],
    "unread_notice_count": 1,
}

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
    "/api/agents": [_AGENT],
    "/api/notices": {
        "open": [_FYI_NOTICE],
        "awaiting": [_AGENT["notices_awaiting_response"]],
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
          // reconnect cycle: close the stream, then a fresh instance re-runs
          // the whole script (the frontend's backoff reopen does `new
          // EventSource(...)` again).
          this._timers.push(setTimeout(() => {
            self.readyState = FakeEventSource.CLOSED;
            if (self.onerror) self.onerror({});
          }, 900));
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


def _open(ctx: BrowserContext, base_url: str, path: str, *, stub_api: bool) -> Page:
    page = ctx.new_page()
    # The fake EventSource is ALWAYS injected (timeline invariants need the
    # stream even against a deployed bundle; the fleet page ignores it).
    page.add_init_script(_FAKE_SSE_JS)
    if stub_api:

        def _stub(route: Route) -> None:
            url = route.request.url
            endpoint = "/api/" + url.split("/api/", 1)[1].split("?", 1)[0]
            body = _API_STUBS.get(endpoint)
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
