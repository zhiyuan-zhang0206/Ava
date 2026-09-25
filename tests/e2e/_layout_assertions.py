"""Reusable Playwright toolkit for the real-browser layout suites.

Two halves, one page definition:

- **The stubbed Home page** (`OVERRIDE_BASE_URL`, `API_STUBS`, `new_context`,
  `open_page`, `wait_layout_settled`): the deterministic page the shell-geometry
  suites drive — a fake EventSource feeds one conversation, `/api/**` answers
  from JSON stubs, and every suite shares the same context/open/settle
  sequence. `test_layout_invariants.py` uses it for I1–I6 and the
  on-screen-keyboard contract.
- **Measurements**: document overflow, viewport containment, center-point
  occlusion, nonempty blocks, settle-before-capture, and the bounded wait that
  absorbs asynchronously mounted panels before declared minimum visible counts
  are probed. Both the layout-invariant suite and the post-deploy visual gate
  consume these so their definitions cannot drift.

Why a fake EventSource: a fresh browser context cannot open the SSE stream
against a cookie-gated deployed cluster, and `page.route` cannot stream to an
EventSource, so without one the timeline surface never grows past the viewport
width and every geometry assertion false-passes. The fake is ALWAYS injected;
target selection is described at `OVERRIDE_BASE_URL` below.
"""

from __future__ import annotations

import json
import os
import time
from typing import TypedDict, cast

from playwright.sync_api import Browser, BrowserContext, Page, Route


class LayoutFailure(TypedDict):
    """One machine-readable structural defect."""

    kind: str
    selector: str
    detail: str
    bbox: dict[str, float] | None


_COUNT_VISIBLE_MATCHES = """
const countVisibleMatches = (selector) => {
  return Array.from(document.querySelectorAll(selector)).filter((element) => {
    element.scrollIntoView({ block: "nearest", inline: "nearest" });
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return element.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}) &&
      style.display !== "none" && style.visibility !== "hidden" &&
      Number(style.opacity) !== 0 && rect.width > 0 && rect.height > 0 &&
      rect.left >= -1 && rect.top >= -1 &&
      rect.right <= window.innerWidth + 1 && rect.bottom <= window.innerHeight + 1;
  }).length;
};
"""

_VISIBLE_COUNT_PROBE = (
    """
(selectors) => {
"""
    + _COUNT_VISIBLE_MATCHES
    + """
  const counts = {};
  for (const selector of selectors) {
    counts[selector] = countVisibleMatches(selector);
  }
  return counts;
}
"""
)

_STRUCTURAL_PROBE = (
    """
({ visibleSelectors, controlSelectors, nonemptySelectors, minimumVisibleCounts }) => {
"""
    + _COUNT_VISIBLE_MATCHES
    + """
  const failures = [];
  const bbox = (rect) => ({
    x: rect.x, y: rect.y, width: rect.width, height: rect.height,
    right: rect.right, bottom: rect.bottom,
  });
  const visible = (element) => {
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return element.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}) &&
      style.display !== "none" && style.visibility !== "hidden" &&
      Number(style.opacity) !== 0 && rect.width > 0 && rect.height > 0;
  };
  const one = (selector, kind) => {
    const element = document.querySelector(selector);
    if (!element) {
      failures.push({ kind, selector, detail: "selector did not match", bbox: null });
      return null;
    }
    return element;
  };

  const scrolling = document.scrollingElement;
  if (!scrolling || scrolling.scrollWidth > scrolling.clientWidth + 1) {
    failures.push({
      kind: "horizontal-overflow", selector: "document.scrollingElement",
      detail: scrolling ? `${scrolling.scrollWidth - scrolling.clientWidth}px overflow` : "missing",
      bbox: null,
    });
  }

  for (const selector of visibleSelectors) {
    const element = one(selector, "visible-panel");
    if (!element) continue;
    element.scrollIntoView({ block: "nearest", inline: "nearest" });
    const rect = element.getBoundingClientRect();
    if (!visible(element) || rect.left < -1 || rect.top < -1 ||
        rect.right > window.innerWidth + 1 || rect.bottom > window.innerHeight + 1) {
      failures.push({
        kind: "visible-panel", selector, detail: "panel is hidden or outside the viewport",
        bbox: bbox(rect),
      });
    }
  }

  for (const selector of controlSelectors) {
    const element = one(selector, "occluded-control");
    if (!element) continue;
    element.scrollIntoView({ block: "nearest", inline: "nearest" });
    const rect = element.getBoundingClientRect();
    const x = Math.max(0, Math.min(window.innerWidth - 1, rect.left + rect.width / 2));
    const y = Math.max(0, Math.min(window.innerHeight - 1, rect.top + rect.height / 2));
    const hit = document.elementFromPoint(x, y);
    if (!visible(element) || !hit || (hit !== element && !element.contains(hit))) {
      failures.push({
        kind: "occluded-control", selector,
        detail: hit ? `covered by ${hit.tagName.toLowerCase()}` : "no center-point hit",
        bbox: bbox(rect),
      });
    }
  }

  for (const selector of nonemptySelectors) {
    const element = one(selector, "empty-block");
    if (!element) continue;
    const hasText = (element.textContent || "").trim().length > 0;
    const hasVisibleChild = Array.from(element.children).some((child) => visible(child));
    if (!hasText && !hasVisibleChild) {
      failures.push({
        kind: "empty-block", selector, detail: "expected content container is empty",
        bbox: bbox(element.getBoundingClientRect()),
      });
    }
  }

  for (const [selector, minimum] of Object.entries(minimumVisibleCounts)) {
    const count = countVisibleMatches(selector);
    if (count < minimum) {
      failures.push({
        kind: "visible-panel", selector,
        detail: `expected ${minimum} visible in-viewport matches, found ${count}`,
        bbox: null,
      });
    }
  }
  return failures;
}
"""
)

_SETTLED_PREDICATE = """
({ readySelector }) => {
  const ready = document.querySelector(readySelector);
  if (!ready) return false;
  const unsettled = [
    ...document.querySelectorAll(
      '[aria-busy="true"], .animate-spin, [data-testid*="skeleton"], [data-testid$="loading"]'
    ),
  ];
  return !unsettled.some((element) => {
    const rect = element.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0 &&
      element.checkVisibility({checkOpacity: true, checkVisibilityCSS: true});
  });
}
"""


def wait_for_layout_settled(page: Page, ready_selector: str, *, timeout_ms: int = 60_000) -> None:
    """Wait until the page is ready and no visible skeleton or spinner remains."""
    page.wait_for_function(
        _SETTLED_PREDICATE,
        arg={"readySelector": ready_selector},
        timeout=timeout_ms,
    )


def visible_in_viewport_counts(page: Page, selectors: tuple[str, ...]) -> dict[str, int]:
    """Count visible in-viewport matches per selector.

    The same rules as the structural probe's minimum-count check, from the one
    shared predicate — a wait built on these counts and the probe's verdict can
    never disagree about what "visible in viewport" means.
    """
    counts = page.evaluate(_VISIBLE_COUNT_PROBE, list(selectors))
    return cast(dict[str, int], counts)


def wait_for_minimum_visible_counts(
    page: Page,
    minimum_counts: dict[str, int],
    *,
    timeout_ms: int = 5_000,
    poll_ms: int = 100,
) -> dict[str, int]:
    """Wait, bounded, until every selector reaches its minimum visible count.

    A panel that mounts only after async data resolves can race an immediate
    structural probe, so a caller that declares minimums first waits for them.
    Returns the counts observed when the wait ended: satisfying the minimums on
    the happy path (the first observation already satisfies them when nothing
    is delayed — no sleep), otherwise the still-short counts at the deadline.
    """
    selectors = tuple(minimum_counts)
    counts = visible_in_viewport_counts(page, selectors)
    deadline = time.monotonic() + timeout_ms / 1000
    while not all(counts[selector] >= minimum for selector, minimum in minimum_counts.items()):
        if time.monotonic() >= deadline:
            break
        page.wait_for_timeout(poll_ms)
        counts = visible_in_viewport_counts(page, selectors)
    return counts


def structural_failures(
    page: Page,
    *,
    visible_selectors: tuple[str, ...] = (),
    control_selectors: tuple[str, ...] = (),
    nonempty_selectors: tuple[str, ...] = (),
    minimum_visible_counts: dict[str, int] | None = None,
) -> list[LayoutFailure]:
    """Measure overflow, viewport placement, occlusion, and expected content.

    Panels and controls are scrolled into view (nearest) before the viewport
    checks: the gate pages scroll inside internal containers, and a panel
    below the fold is reachable content, not a defect.
    """
    result = page.evaluate(
        _STRUCTURAL_PROBE,
        {
            "visibleSelectors": visible_selectors,
            "controlSelectors": control_selectors,
            "nonemptySelectors": nonempty_selectors,
            "minimumVisibleCounts": minimum_visible_counts or {},
        },
    )
    return cast(list[LayoutFailure], result)


def no_document_horizontal_overflow(page: Page) -> bool:
    """Return whether the document fits its horizontal viewport."""
    return not any(
        failure["kind"] == "horizontal-overflow" for failure in structural_failures(page)
    )


def element_within_viewport(page: Page, selector: str) -> bool:
    """Return whether one required panel is visible and wholly in the viewport."""
    return not structural_failures(page, visible_selectors=(selector,))


def element_within_parent(page: Page, selector: str) -> bool:
    """Return whether an element's scroll width fits its parent."""
    return bool(
        page.evaluate(
            """(selector) => {
              const element = document.querySelector(selector);
              return Boolean(element && element.parentElement &&
                element.scrollWidth <= element.parentElement.clientWidth + 1);
            }""",
            selector,
        )
    )


def all_elements_within_parents(page: Page, selector: str) -> bool:
    """Return whether at least one match exists and every match fits its parent."""
    return bool(
        page.evaluate(
            """(selector) => {
              const elements = document.querySelectorAll(selector);
              return elements.length > 0 && Array.from(elements).every((element) =>
                element.parentElement && element.scrollWidth <= element.parentElement.clientWidth + 1
              );
            }""",
            selector,
        )
    )


# ── The stubbed Home page ──────────────────────────────────────────────
# The deterministic page the shell-geometry suites drive: a fake EventSource
# feeds one conversation, `/api/**` answers from API_STUBS, and open_page gives
# every suite the same context/open/settle sequence.


# `AVA_MOBILE_TEST_BASE_URL` points new_context at the deployed bundle (session
# cookie via AVA_TEST_SESSION_COOKIE); unset, the test module's `frontend_target`
# fixture serves the session `frontend_proc` build.
OVERRIDE_BASE_URL = os.environ.get("AVA_MOBILE_TEST_BASE_URL")


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
API_STUBS: dict[str, object] = {
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


def new_context(browser: Browser, width: int) -> BrowserContext:
    ctx = browser.new_context(
        viewport={"width": width, "height": 664},
        device_scale_factor=2 if width < 768 else 1,
    )
    if OVERRIDE_BASE_URL and os.environ.get("AVA_TEST_SESSION_COOKIE"):
        cookie = os.environ["AVA_TEST_SESSION_COOKIE"].split("=", 1)[-1]
        host = OVERRIDE_BASE_URL.split("//", 1)[-1].split(":", 1)[0]
        ctx.add_cookies([{"name": "ava_session", "value": cookie, "domain": host, "path": "/"}])
    return ctx


def open_page(
    ctx: BrowserContext,
    base_url: str,
    path: str,
    *,
    stub_api: bool,
    reconnect_sse: bool = True,
    open_inspector: bool = False,
    extra_init_script: str | None = None,
) -> Page:
    page = ctx.new_page()
    # The fake EventSource is ALWAYS injected (timeline invariants need the
    # stream even against a deployed bundle; the fleet page ignores it).
    page.add_init_script(_FAKE_SSE_JS.replace("__RECONNECT__", json.dumps(reconnect_sse)))
    if extra_init_script is not None:
        # Suites that need another pre-app hook (e.g. the mobile-keyboard
        # suite's fake visualViewport) pass it here: init scripts run before
        # the app's own code, in the order they were added.
        page.add_init_script(extra_init_script)
    if stub_api:

        def _stub(route: Route) -> None:
            url = route.request.url
            endpoint = "/api/" + url.split("/api/", 1)[1].split("?", 1)[0]
            body = (
                {"settings": [{"key": "display.inspector_open", "value": True}]}
                if open_inspector and endpoint == "/api/settings"
                else API_STUBS.get(endpoint)
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


def wait_layout_settled(page: Page) -> None:
    """Wait for the layout-driving element, then a beat for React + SSE."""
    page.wait_for_selector("textarea, [role='tablist']", timeout=15_000)
    page.wait_for_timeout(1500)
