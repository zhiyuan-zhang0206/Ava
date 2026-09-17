"""Page-level visual + alignment gate for the multi-agent compare view (task #3802).

Renders /insights/compare with three lanes over a frozen clock and a fully
stubbed API. Two contracts:

- alignment: every lane's time axis maps a delivery timestamp to the same x
  (the compare view pins ONE shared canvas width across lanes), so the message
  arrows cannot drift between lanes;
- layout: every canvas fits its container, a lane's detail panel narrows all
  lanes together, and the narrowed lane keeps its edge ticks clear of the panel;
- stacking: the panel card paints above the cross-lane arrow overlay (below
  `lg` the panel stacks under its lane, where arrows cross its band). No
  screenshot golden is committed: this repo mints visual references only
  through the Visual baselines workflow's fixed reference set, and this page is
  still under active iteration, so the numeric contracts are the gate.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import TypedDict, cast

import pytest
from playwright.sync_api import Browser, BrowserContext, Page, Route

from tests.e2e._ports import FRONTEND_URL

_AGENT_ORDER = (101, 102, 103)
_FROZEN_NOW = datetime(2026, 9, 17, 18, 0, 0, tzinfo=UTC)
_WINDOW = {"from": "2026-09-17T17:30:00Z", "to": "2026-09-17T18:00:00Z"}
_WINDOW_START = datetime(2026, 9, 17, 17, 30, tzinfo=UTC)
_WINDOW_SPAN_S = 1800.0

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


def _agent(agent_id: int) -> dict[str, object]:
    base = "2026-09-01T00:00:00Z"
    return {
        "agent_id": agent_id,
        "spawner": "user",
        "fork_source_agent_id": None,
        "status": "idling",
        "pid": 100 + agent_id,
        "spawned_at": base,
        "started_at": base,
        "last_active_at": base,
        "last_inbound_at": base,
        "label": f"lane agent {agent_id}",
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


def _turn_row(turn: int, offset_s: int, duration_s: int) -> dict[str, object]:
    def stamp(value_s: int) -> str:
        return (_WINDOW_START + timedelta(seconds=value_s)).strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "turn": turn,
        "n_turns": 1,
        "start": stamp(offset_s),
        "end": stamp(offset_s + duration_s),
        "active_s": duration_s,
        "trace_id": f"trace-{turn}",
        "checkpoint_id": None,
        "ok": True,
        "llm": {
            "calls": 2,
            "in_total": 1200,
            "cache_read": 800,
            "out_total": 90,
            "reasoning": 20,
            "latency_ms": 1800,
            "cost_usd": 0.01,
            "model": "deepseek-v4-flash",
        },
        "execs": [],
        "anomalies": [],
        "tags": [],
    }


def _inbounds() -> dict[int, list[dict[str, object]]]:
    """23 raw deliveries; same-pair bursts collapse into count badges."""
    events: dict[int, list[dict[str, object]]] = {101: [], 102: [], 103: []}
    inbound_id = 9000

    def add(target: int, source: int, base_s: int, count: int, step_s: int) -> None:
        nonlocal inbound_id
        for index in range(count):
            stamp = _WINDOW_START + timedelta(seconds=base_s + index * step_s)
            events[target].append(
                {
                    "ts": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "source": f"agent:{source}",
                    "inbound_id": inbound_id,
                }
            )
            inbound_id += 1

    add(102, 101, 6 * 60 + 10, 3, 1)  # 17:36:10 x3 -> one count-3 arrow
    add(102, 101, 14 * 60, 1, 0)  # 17:44:00 lone arrow
    add(103, 101, 10 * 60, 8, 1)  # 17:40:00 x8 -> one count-8 arrow
    add(103, 102, 16 * 60 + 30, 3, 2)  # 17:46:30 x3
    add(103, 102, 17 * 60 + 50, 3, 1)  # 17:47:50 x3 (separate cluster)
    add(101, 103, 22 * 60 + 30, 5, 1)  # 17:52:30 x5
    return events


def _timeline(agent_id: int, inbounds: list[dict[str, object]]) -> dict[str, object]:
    offsets = {
        101: (2 * 60, 10 * 60 + 30, 20 * 60),
        102: (5 * 60, 15 * 60, 25 * 60),
        103: (8 * 60, 17 * 60, 28 * 60),
    }[agent_id]
    rows = [_turn_row(turn, offset, 60) for turn, offset in enumerate(offsets, start=1)]
    return {
        "agent_id": agent_id,
        "window": _WINDOW,
        "meta": {
            "n_turns": len(rows),
            "wall_span_s": _WINDOW_SPAN_S,
            "active_s": len(rows) * 60,
            "tokens_in": 1200 * len(rows),
            "tokens_out": 90 * len(rows),
            "cost_usd": 0.01 * len(rows),
            "n_exec_failed": 0,
            "n_compact": 0,
            "n_restart": 0,
            "fallback_turns": 0,
            "unmatched_turns": 0,
        },
        "rows": rows,
        "events": [],
        "boundaries": {
            "initialize_turn": None,
            "last_before_compact_turn": None,
            "post_window_turns": 0,
            "has_activity_after_window": False,
        },
        "inbounds": inbounds,
    }


def _stubs() -> dict[str, object]:
    inbounds = _inbounds()
    agents = [_agent(agent_id) for agent_id in _AGENT_ORDER]
    stubs: dict[str, object] = {
        "/api/auth/check": {"authenticated": True},
        "/api/settings": {"settings": []},
        "/api/config": {"fields": [], "raw_overrides": {}, "machine_capabilities": []},
        "/api/agents": {"agents": agents, "next_cursor": None},
        "/api/agents/roster": {"agents": agents, "ancestors": []},
        "/api/notices": {"open": [], "awaiting": [], "resolved_page": [], "next_cursor": None},
        "/api/tasks": {"tasks": []},
    }
    for agent_id in _AGENT_ORDER:
        stubs[f"/api/agents/{agent_id}/run-timeline"] = _timeline(agent_id, inbounds[agent_id])
    return stubs


def _stub_route(stubs: dict[str, object]) -> Callable[[Route], None]:
    def _stub(route: Route) -> None:
        endpoint = "/api/" + route.request.url.split("/api/", 1)[1].split("?", 1)[0]
        route.fulfill(
            status=200, content_type="application/json", body=json.dumps(stubs.get(endpoint, {}))
        )

    return _stub


def _new_compare_page(browser: Browser, *, width: int, height: int) -> tuple[BrowserContext, Page]:
    """A compare page on the fixture stubs. A frozen Date makes the page's
    initial window deterministic: the default half-hour slice always lands on
    [17:30, 18:00) of the fixture timeline."""
    context = browser.new_context(
        viewport={"width": width, "height": height},
        color_scheme="light",
        locale="en-US",
        timezone_id="UTC",
    )
    page = context.new_page()
    page.add_init_script(_INERT_EVENT_SOURCE)
    page.clock.set_fixed_time(_FROZEN_NOW)
    page.route("**/api/**", _stub_route(_stubs()))
    return context, page


@pytest.fixture
def compare_page(frontend_proc: None, playwright_browser: Browser) -> Iterator[Page]:
    context, page = _new_compare_page(playwright_browser, width=1280, height=900)
    try:
        yield page
    finally:
        context.close()


_ALIGNMENT_SCRIPT = """
() => {
  const svg = document.querySelector('[data-testid="compare-arrows"]');
  const svgRect = svg.getBoundingClientRect();
  const lanes = Array.from(document.querySelectorAll('[data-testid="run-timeline-visualization"]'));
  return {
    mediaWide: window.matchMedia("(min-width: 1024px)").matches,
    laneWidths: lanes.map((el) => el.getBoundingClientRect().width),
    laneLefts: lanes.map((el) => el.getBoundingClientRect().left - svgRect.left),
    containerWidths: lanes.map((el) => el.parentElement.getBoundingClientRect().width),
    tickLefts: lanes.map((el) =>
      Array.from(el.querySelectorAll('[data-timeline-tick]'), (tick) => tick.style.left),
    ),
    arrows: Array.from(document.querySelectorAll('[data-testid="compare-arrow"]')).map((el) => ({
      ts: el.getAttribute('data-ts'),
      count: Number(el.getAttribute('data-count')),
      x: Number(el.getAttribute('data-x')),
      target: Number(el.getAttribute('data-target')),
    })),
    badges: Array.from(document.querySelectorAll('[data-testid="compare-arrow-count"]')).map(
      (el) => el.textContent,
    ),
    horizontalOverflow: document.documentElement.scrollWidth - window.innerWidth,
  };
}
"""


def _open_compare(page: Page) -> None:
    page.goto(
        f"{FRONTEND_URL}/insights/compare?agents=" + ",".join(str(a) for a in _AGENT_ORDER),
        wait_until="domcontentloaded",
    )
    page.wait_for_function(
        "() => document.querySelectorAll('[data-testid=\"compare-arrow\"]').length === 6",
        timeout=15_000,
    )
    _wait_for_shared_widths(page)


_SHARED_WIDTHS_SCRIPT = """
(below) => {
  const lanes = Array.from(document.querySelectorAll('[data-testid="run-timeline-visualization"]'));
  if (lanes.length !== 3) return false;
  const widths = lanes.map((lane) => lane.getBoundingClientRect().width);
  if (new Set(widths).size !== 1) return false;
  return below === null || widths[0] < below - 0.5;
}
"""


def _wait_for_shared_widths(page: Page, *, below: float | None = None) -> None:
    """The shared width settles one React round trip after a panel toggle
    (chart -> compare -> chart), so poll instead of sampling once."""
    page.wait_for_function(_SHARED_WIDTHS_SCRIPT, arg=below, timeout=5_000)


def _expected_x(*, lane_left: float, lane_width: float, ts: str) -> float:
    stamp = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    fraction = (stamp - _WINDOW_START).total_seconds() / _WINDOW_SPAN_S
    return lane_left + 32 + fraction * (lane_width - 64)


def test_compare_alignment_and_arrows(compare_page: Page) -> None:
    """One shared axis: equal canvases, equal ticks, and arrows on the ts x."""
    _open_compare(compare_page)
    state = compare_page.evaluate(_ALIGNMENT_SCRIPT)

    assert len(state["laneWidths"]) == len(_AGENT_ORDER)
    assert len(set(state["laneWidths"])) == 1, state["laneWidths"]
    assert len({tuple(ticks) for ticks in state["tickLefts"]}) == 1, state["tickLefts"]

    lane_index = {agent_id: index for index, agent_id in enumerate(_AGENT_ORDER)}
    for arrow in state["arrows"]:
        lane = lane_index[arrow["target"]]
        expected = _expected_x(
            lane_left=state["laneLefts"][lane],
            lane_width=state["laneWidths"][lane],
            ts=arrow["ts"],
        )
        assert abs(arrow["x"] - expected) <= 1.5, (arrow, expected)

    assert sorted(arrow["count"] for arrow in state["arrows"]) == [1, 3, 3, 3, 5, 8]
    assert len(state["badges"]) == 5
    assert state["horizontalOverflow"] <= 1

    # Every lane's edge tick labels sit inside the visible canvas (the canvas
    # fits its container), so the rightmost timestamp is not clipped.
    for lane in compare_page.locator('[data-testid="run-timeline-visualization"]').all():
        container_box = lane.locator("xpath=..").bounding_box()
        lane_box = lane.bounding_box()
        assert container_box is not None and lane_box is not None
        assert lane_box["width"] <= container_box["width"] + 0.5
        ticks = lane.locator("[data-timeline-tick]")
        assert ticks.count() == 5
        for index in range(ticks.count()):
            tick_box = ticks.nth(index).bounding_box()
            assert tick_box is not None
            assert (
                tick_box["x"] + tick_box["width"]
                <= container_box["x"] + container_box["width"] + 0.5
            )


def test_compare_panel_keeps_shared_canvas_width(compare_page: Page) -> None:
    """Opening one lane's detail panel narrows every lane together."""
    _open_compare(compare_page)
    before = compare_page.evaluate(_ALIGNMENT_SCRIPT)

    compare_page.locator('button[aria-label^="Turn "]').nth(3).click()
    compare_page.wait_for_selector('[role="region"][aria-label="Turn details"]')
    _wait_for_shared_widths(compare_page, below=before["laneWidths"][0])
    after = compare_page.evaluate(_ALIGNMENT_SCRIPT)

    assert len(set(after["laneWidths"])) == 1, after["laneWidths"]
    assert after["laneWidths"][0] < before["laneWidths"][0], (before, after)

    # The narrowed lane's edge tick labels must stay inside the canvas and
    # clear of the detail panel they sit next to (~12px gap).
    panel_box = compare_page.locator('[role="region"][aria-label="Turn details"]').bounding_box()
    lane_canvas = compare_page.locator('[data-testid="run-timeline-visualization"]').nth(1)
    lane_box = lane_canvas.bounding_box()
    assert panel_box is not None and lane_box is not None
    ticks = lane_canvas.locator("[data-timeline-tick]")
    tick_count = ticks.count()
    assert tick_count == 5
    for index in range(tick_count):
        tick_box = ticks.nth(index).bounding_box()
        assert tick_box is not None
        assert tick_box["x"] + tick_box["width"] <= lane_box["x"] + lane_box["width"] + 0.5
        assert tick_box["x"] + tick_box["width"] <= panel_box["x"] + 0.5


class _LayeringReport(TypedDict):
    """The narrow-layout probe: how many arrow-curve sample points fall inside
    the open panel's box, and which element paints on top at each one."""

    samples: int
    coveredByOverlay: int
    onPanel: int
    offenders: list[dict[str, int | str]]


_NARROW_LAYERING_SCRIPT = """
() => {
  const panel = document.querySelector('[role="region"][aria-label="Turn details"]');
  const panelRect = panel.getBoundingClientRect();
  const overlay = document.querySelector('[data-testid="compare-arrows"]');
  const overlayRect = overlay.getBoundingClientRect();
  const report = { samples: 0, coveredByOverlay: 0, onPanel: 0, offenders: [] };
  for (const path of document.querySelectorAll('[data-testid="compare-arrow"]')) {
    const length = path.getTotalLength();
    for (let at = 0; at <= length; at += 4) {
      const point = path.getPointAtLength(at);
      const x = overlayRect.left + point.x;
      const y = overlayRect.top + point.y;
      if (x < panelRect.left || x > panelRect.right || y < panelRect.top || y > panelRect.bottom) {
        continue;
      }
      report.samples += 1;
      const top = document.elementFromPoint(x, y);
      if (top && top.closest('[data-testid="compare-arrows"]')) {
        report.coveredByOverlay += 1;
        if (report.offenders.length < 3) {
          report.offenders.push({
            ts: path.getAttribute('data-ts'),
            x: Math.round(x),
            y: Math.round(y),
          });
        }
      } else if (top && top.closest('[role="region"][aria-label="Turn details"]')) {
        report.onPanel += 1;
      }
    }
  }
  return report;
}
"""


def _settle_narrow_layering(page: Page) -> _LayeringReport:
    """Poll until the overlay has re-measured against the opened panel (the
    panel's height shifts every lane below it). Bounded: the samples assertion
    fails loudly if no crossing arrow ever reaches the panel."""
    report = cast(_LayeringReport, page.evaluate(_NARROW_LAYERING_SCRIPT))
    for _ in range(20):
        if report["samples"] >= 4:
            break
        page.wait_for_timeout(100)
        report = cast(_LayeringReport, page.evaluate(_NARROW_LAYERING_SCRIPT))
    return report


def test_compare_narrow_panel_paints_above_arrows(
    frontend_proc: None, playwright_browser: Browser
) -> None:
    """Below lg the panel stacks under its lane (task #3825): the cross-lane
    arrow overlay must stay behind the panel card."""
    context, page = _new_compare_page(playwright_browser, width=900, height=1600)
    try:
        _open_compare(page)
        page.locator('button[aria-label^="Turn "]').nth(3).click()
        panel = page.locator('[role="region"][aria-label="Turn details"]')
        panel.wait_for()
        panel.scroll_into_view_if_needed()
        report = _settle_narrow_layering(page)
        assert report["samples"] >= 4, report
        assert report["coveredByOverlay"] == 0, report
        assert report["onPanel"] == report["samples"], report
    finally:
        context.close()
