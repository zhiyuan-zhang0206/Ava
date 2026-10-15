from __future__ import annotations

import re
import time
from typing import TypedDict

import httpx
import pytest
from playwright.sync_api import Page, Route

from tests.e2e._settings import pin_expand_runs_all
from tests.e2e.fixture_environment import E2EEnv

_SAMPLER_JS = """
() => {
  const all = [...document.querySelectorAll('[data-slot="scroll-area-viewport"]')];
  const vp = all.find(v => v.querySelectorAll('[data-item-id]').length > 0);
  if (!vp) return {error: 'no timeline viewport'};
  window.__tl = { samples: [], vp };
  const sample = (why) => {
    const items = [...vp.querySelectorAll('[data-item-id]')];
    const vpRect = vp.getBoundingClientRect();
    // The reading position = the TOPMOST VISIBLE node's viewport top (the
    // 0.0 prompt card included — when it fills the viewport the user reads
    // inside it and the invariant holds there, not on the first real item).
    let anchorId = null, anchorTop = null;
    for (const n of items) {
      const r = n.getBoundingClientRect();
      if (r.bottom >= vpRect.top && r.top <= vpRect.bottom) { anchorId = n.dataset.itemId; anchorTop = r.top; break; }
    }
    if (window.__tl.samples.length > 20000) return;
    const reserve = vp.querySelector('[data-history-pull-space]');
    const reserveBox = reserve?.getBoundingClientRect();
    const exposedPullSpace = reserveBox ? Math.max(0, Math.min(reserveBox.height, reserveBox.bottom - vpRect.top)) : 0;
    window.__tl.samples.push({ t: Math.round(performance.now()), why, st: vp.scrollTop, sh: vp.scrollHeight, n: Number(vp.querySelector('[role=log]').dataset.timelineItemCount), anchorId, anchorTop, exposedPullSpace });
  };
  let raf = 0;
  const loop = () => { sample('raf'); raf = requestAnimationFrame(loop); };
  raf = requestAnimationFrame(loop);
  window.__tl.stop = () => cancelAnimationFrame(raf);
  vp.addEventListener('scroll', () => sample('scroll'), { passive: true });
  sample('init');
  return {ok: true, n: Number(vp.querySelector('[role=log]').dataset.timelineItemCount), sh: vp.scrollHeight};
}
"""

_SCROLL_UP_JS = """
() => {
  const vp = window.__tl.vp;
  const boundary = vp.querySelector('[data-history-pull-space]').getBoundingClientRect().height;
  window.__tl.arrivalEnded = vp.scrollTop <= boundary;
  const ended = event => {
    if (event.target === vp && vp.scrollTop <= boundary + 1) {
      window.__tl.arrivalEnded = true;
      vp.removeEventListener('scrollend', ended);
    }
  };
  vp.addEventListener('scrollend', ended);
  let st = vp.scrollTop;
  const step = () => {
    st = Math.max(boundary, st - 500);
    vp.scrollTop = st;
    if (st > boundary) setTimeout(step, 30);
  };
  step();
  return 'scrolling';
}
"""


class _ReadingSample(TypedDict):
    anchorId: str | None
    anchorTop: float
    exposedPullSpace: float


def _reading_top(sample: _ReadingSample) -> float:
    return sample["anchorTop"] - (sample["exposedPullSpace"] if sample["anchorId"] == "0.0" else 0)


def _assert_landing_holds(page: Page, sample_start: int, round_no: int) -> None:
    samples = page.evaluate("window.__tl.samples")[sample_start:]
    landing_idx = next(
        (i for i, s in enumerate(samples) if i > 0 and s["n"] != samples[i - 1]["n"]),
        None,
    )
    assert landing_idx is not None, f"round {round_no}: no landing frame in the samples"
    before = [s for s in samples[:landing_idx] if s["anchorTop"] is not None]
    after = [s for s in samples[landing_idx:] if s["anchorTop"] is not None]
    assert before and after, f"round {round_no}: no anchor measurements around the landing"
    before_top = _reading_top(before[-1])
    for s in after:
        top = _reading_top(s)
        assert abs(top - before_top) < 3.0, (
            f"round {round_no}: reading position moved by "
            f"{top - before_top:.1f}px beyond pull-space motion across the landing "
            f"(before={before_top:.1f}, after={top:.1f}, "
            f"st={s['st']}, sh={s['sh']}, n={s['n']}, anchor={s['anchorId']})"
        )
    assert after[-1]["exposedPullSpace"] < 1, "the released pull left a blank space above history"


def _wait_landing(
    page: Page,
    before_n: int,
    round_no: int,
    agent_id: int,
    gateway_url: str,
    before_requests: list[str],
) -> int:
    deadline = time.monotonic() + 30.0
    n = before_n
    while time.monotonic() < deadline:
        n = page.evaluate(
            "Number(window.__tl.vp.querySelector('[role=log]').dataset.timelineItemCount)"
        )
        if n > before_n:
            return n
        page.wait_for_timeout(200)
    rest_items = httpx.get(
        f"{gateway_url}/api/agents/{agent_id}/timeline?limit=1000",
        timeout=30.0,
    ).json()["items"]
    samples = page.evaluate("window.__tl.samples")
    raise AssertionError(
        f"round {round_no}: the older window never landed: "
        f"rest_total={len(rest_items)}, before_requests={before_requests}, "
        f"last_samples={samples[-6:]}"
    )


def _run_rounds(
    page: Page,
    before_requests: list[str],
    paused_routes: list[Route],
    init_n: int,
    agent_id: int,
    gateway_url: str,
) -> int:
    sample_start = page.evaluate("window.__tl.samples.length")
    _load_older_at_top(page, before_requests, paused_routes)
    _wait_landing(page, init_n, 1, agent_id, gateway_url, before_requests)
    page.wait_for_timeout(1200)
    _assert_landing_holds(page, sample_start, 1)

    round_no = 1
    while round_no < 5:
        round_no += 1
        page.evaluate("window.__tl.vp.scrollTop = 240")
        page.evaluate("window.__tl.vp.dispatchEvent(new Event('scroll'))")
        page.wait_for_timeout(200)
        before_n = page.evaluate(
            "Number(window.__tl.vp.querySelector('[role=log]').dataset.timelineItemCount)"
        )
        sample_start = page.evaluate("window.__tl.samples.length")
        did_pull = _pull_history_page(page, before_requests)

        deadline = time.monotonic() + (30.0 if did_pull else 0.0)
        n = before_n
        while time.monotonic() < deadline:
            n = page.evaluate(
                "Number(window.__tl.vp.querySelector('[role=log]').dataset.timelineItemCount)"
            )
            if n > before_n:
                break
            page.wait_for_timeout(200)
        if n <= before_n:
            rest = httpx.get(
                f"{gateway_url}/api/agents/{agent_id}/timeline?limit=1000",
                timeout=30.0,
            ).json()["items"]
            if before_n < len(rest):
                raise AssertionError(
                    f"round {round_no}: a released pull did not load more history; "
                    f"mounted={before_n} rest_total={len(rest)}, "
                    f"last_samples={page.evaluate('window.__tl.samples')[-6:]}"
                )
            break
        page.wait_for_timeout(1200)
        _assert_landing_holds(page, sample_start, round_no)
    return round_no


def _pull_history_page(page: Page, before_requests: list[str]) -> bool:
    count = len(before_requests)
    page.evaluate(_SCROLL_UP_JS)
    page.wait_for_function("() => window.__tl.arrivalEnded", timeout=15_000)
    assert len(before_requests) == count, "arriving at the top loaded history without a pull"
    if page.get_by_test_id("load-older-keyboard").count() == 0:
        return False
    viewport = page.locator('[data-slot="scroll-area-viewport"]').filter(
        has=page.locator('[role="log"]')
    )
    box = viewport.bounding_box()
    assert box is not None
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    distance = viewport.locator("[data-history-pull-space]").evaluate(
        "el => el.getBoundingClientRect().height"
    )
    page.mouse.wheel(0, -distance)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and len(before_requests) == count:
        page.wait_for_timeout(50)
    assert len(before_requests) == count + 1, "a released pull must request exactly one page"
    return True


def _load_older_at_top(page: Page, before_requests: list[str], paused_routes: list[Route]) -> None:
    assert _pull_history_page(page, before_requests), "the initial history page is unavailable"

    assert len(paused_routes) == 1, "the first paging request was not held"
    before_n = page.evaluate(
        "Number(window.__tl.vp.querySelector('[role=log]').dataset.timelineItemCount)"
    )

    page.evaluate("window.__tl.vp.scrollTop = 600")
    page.evaluate("window.__tl.vp.dispatchEvent(new Event('scroll'))")
    assert page.evaluate("window.__tl.vp.scrollTop") == 600
    assert (
        page.evaluate(
            "Number(window.__tl.vp.querySelector('[role=log]').dataset.timelineItemCount)"
        )
        == before_n
    ), "older history landed before the in-flight user scroll"
    paused_routes.pop().continue_()


@pytest.mark.scenario("tests.e2e.fakes.scenarios.load_older:build")
def test_load_older_preserves_reading_position(e2e_env: E2EEnv) -> None:
    page = e2e_env.page
    agent_id = e2e_env.agent_id

    before_requests: list[str] = []
    paused_routes: list[Route] = []

    def pause_first_page(route: Route) -> None:
        before_requests.append(route.request.url)
        if len(before_requests) == 1:
            paused_routes.append(route)
        else:
            route.continue_()

    page.route(re.compile(r"/api/agents/\d+/timeline\?.*before="), pause_first_page)

    pin_expand_runs_all(e2e_env.gateway_url)

    page.goto(e2e_env.agent_url)
    page.wait_for_selector('[data-testid="sse-ready"]', state="attached", timeout=10_000)

    page.fill('[data-testid="composer-input"]', "\u8dd1\u5b8c\u5168\u90e8\u6b65\u9aa4\u3002")
    page.click('[data-testid="composer-send"]')
    deadline = time.monotonic() + 120.0
    while True:
        items = httpx.get(
            f"{e2e_env.gateway_url}/api/agents/{agent_id}/timeline?limit=1000",
            timeout=90.0,
        ).json()["items"]
        if (
            any(
                it["kind"] == "agent_chat"
                and "\u5168\u90e8\u6267\u884c\u5b8c\u6bd5" in it["payload"]
                for it in items
            )
            or time.monotonic() > deadline
        ):
            break
        time.sleep(0.5)

    page.reload()
    page.wait_for_selector('[data-testid="sse-ready"]', state="attached", timeout=10_000)

    n = 0
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        n = page.evaluate(
            "Number(document.querySelector('[role=log]')?.dataset.timelineItemCount ?? 0)"
        )
        if n > 50:
            break
        page.wait_for_timeout(300)
    assert n >= 50, f"scenario timeline too short to page: {n} items"

    init = page.evaluate(_SAMPLER_JS)
    assert init.get("ok"), init

    rounds = _run_rounds(
        page, before_requests, paused_routes, init["n"], agent_id, e2e_env.gateway_url
    )
    assert rounds >= 2, f"expected multiple load-older rounds, got {rounds}"
