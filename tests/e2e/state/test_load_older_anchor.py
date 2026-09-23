"""Load-older scroll anchor (#1272) — reading-position preservation in the real
browser.

The user scrolls up to read older history; the previous window is fetched and
prepended. The reading position (the topmost visible message) must not move —
not even when the user keeps scrolling while the fetch is in flight (the old
code pinned the compensation to the trigger-time viewport top, so every
landing yanked the viewport back by the distance scrolled in between; with
the anchor below the viewport it scrolled by the anchor's whole displacement
— the "whole list jumps after loading older messages" report).

The paging request is triggered by reaching the top (auto-load, task #4186):
scrolling up to scrollTop=0 with older pages remaining fires the fetch for
the previous window — no control, no pull gesture. The request is slowed with
a route delay so the user's continued scrolling happens while the fetch is
in flight, deterministically.
"""

from __future__ import annotations

import re
import time

import httpx
import pytest
from playwright.sync_api import Page

from tests.e2e._env import E2EEnv
from tests.e2e._settings import pin_expand_runs_all

# Records (scrollTop, scrollHeight, item count, first real item + its viewport
# top) on every frame + scroll event, into window.__tl.samples.
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
    window.__tl.samples.push({ t: Math.round(performance.now()), why, st: vp.scrollTop, sh: vp.scrollHeight, n: items.length, anchorId, anchorTop });
  };
  let raf = 0;
  const loop = () => { sample('raf'); raf = requestAnimationFrame(loop); };
  raf = requestAnimationFrame(loop);
  window.__tl.stop = () => cancelAnimationFrame(raf);
  vp.addEventListener('scroll', () => sample('scroll'), { passive: true });
  sample('init');
  return {ok: true, n: vp.querySelectorAll('[data-item-id]').length, sh: vp.scrollHeight};
}
"""

_SCROLL_UP_JS = """
() => {
  const vp = window.__tl.vp;
  let st = vp.scrollTop;
  const step = () => {
    st = Math.max(0, st - 500);
    vp.scrollTop = st;
    if (st > 0) setTimeout(step, 30);
  };
  step();
  return 'scrolling';
}
"""


def _assert_landing_holds(page: Page, sample_start: int, round_no: int) -> None:
    """The reading position is the first real item's viewport top. Locate the
    landing frame (the first sample whose item count differs from the previous
    one) and require the anchor to have stayed put across it — a > 3 px move
    is the #1272 yank / #2623 under-compensation."""
    samples = page.evaluate("window.__tl.samples")[sample_start:]
    landing_idx = next(
        (i for i, s in enumerate(samples) if i > 0 and s["n"] != samples[i - 1]["n"]),
        None,
    )
    assert landing_idx is not None, f"round {round_no}: no landing frame in the samples"
    before = [s for s in samples[:landing_idx] if s["anchorTop"] is not None]
    after = [s for s in samples[landing_idx:] if s["anchorTop"] is not None]
    assert before and after, f"round {round_no}: no anchor measurements around the landing"
    before_top = before[-1]["anchorTop"]
    for s in after:
        assert abs(s["anchorTop"] - before_top) < 3.0, (
            f"round {round_no}: reading position moved by "
            f"{s['anchorTop'] - before_top:.1f}px across the load-older landing "
            f"(before={before_top:.1f}, after={s['anchorTop']:.1f}, "
            f"st={s['st']}, sh={s['sh']}, n={s['n']}, anchor={s['anchorId']})"
        )


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
        n = page.evaluate("window.__tl.vp.querySelectorAll('[data-item-id]').length")
        if n > before_n:
            return n  # the older window landed
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
    page: Page, before_requests: list[str], init_n: int, agent_id: int, gateway_url: str
) -> int:
    """Round 1 uses the first top arrival (with the in-flight-scroll scenario);
    rounds 2+ leave the top and re-arrive — reaching the top auto-fires the
    next window (task #4186; the QA #2623 repro path). Each round must hold
    < 3px; the above-anchor stack grows every round, which is exactly where
    the estimate-based under-compensation lived. Returns the number of rounds
    completed."""
    sample_start = page.evaluate("window.__tl.samples.length")
    _load_older_at_top(page, before_requests)
    _wait_landing(page, init_n, 1, agent_id, gateway_url, before_requests)
    page.wait_for_timeout(1200)
    _assert_landing_holds(page, sample_start, 1)

    round_no = 1
    while round_no < 5:
        round_no += 1
        # Leave the top, then come back — a fresh arrival fires the next window.
        page.evaluate("window.__tl.vp.scrollTop = 240")
        page.evaluate("window.__tl.vp.dispatchEvent(new Event('scroll'))")
        page.wait_for_timeout(200)
        before_n = page.evaluate("window.__tl.vp.querySelectorAll('[data-item-id]').length")
        sample_start = page.evaluate("window.__tl.samples.length")
        page.evaluate("window.__tl.vp.scrollTop = 0")
        page.evaluate("window.__tl.vp.dispatchEvent(new Event('scroll'))")

        deadline = time.monotonic() + 30.0
        n = before_n
        while time.monotonic() < deadline:
            n = page.evaluate("window.__tl.vp.querySelectorAll('[data-item-id]').length")
            if n > before_n:
                break
            page.wait_for_timeout(200)
        if n <= before_n:
            # No landing: either history is exhausted (everything the backend
            # has is already mounted) or the auto-load failed to fire.
            rest = httpx.get(
                f"{gateway_url}/api/agents/{agent_id}/timeline?limit=1000",
                timeout=30.0,
            ).json()["items"]
            if before_n < len(rest):
                raise AssertionError(
                    f"round {round_no}: reaching the top did not load more history; "
                    f"mounted={before_n} rest_total={len(rest)}, "
                    f"last_samples={page.evaluate('window.__tl.samples')[-6:]}"
                )
            break  # history exhausted — no more rounds
        page.wait_for_timeout(1200)
        _assert_landing_holds(page, sample_start, round_no)
    return round_no


def _load_older_at_top(page: Page, before_requests: list[str]) -> None:
    """Trigger load-older by reaching the top (auto-load, task #4186): scroll
    up to scrollTop=0 — the resulting scroll event fires the fetch for the
    previous window. Then keep scrolling while the route-delayed fetch is in
    flight."""
    page.evaluate(_SCROLL_UP_JS)
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        st = page.evaluate("window.__tl.vp.scrollTop")
        if st == 0:
            break
        page.wait_for_timeout(100)
    assert page.evaluate("window.__tl.vp.scrollTop") == 0, "viewport never reached the top"

    # Reaching the top fires the request; the route handler records it before
    # its 2s delay, so its presence proves the trigger.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not before_requests:
        page.wait_for_timeout(50)
    assert before_requests, "reaching the top never triggered the load-older request"

    # The user keeps scrolling while the delayed fetch is in flight (the
    # #1272 yank happened exactly when the landing compensated against a
    # stale trigger-time position).
    page.evaluate("window.__tl.vp.scrollTop = 600")
    page.evaluate("window.__tl.vp.dispatchEvent(new Event('scroll'))")


@pytest.mark.scenario("tests.e2e.fakes.scenarios.load_older:build")
def test_load_older_preserves_reading_position(e2e_env: E2EEnv) -> None:
    page = e2e_env.page
    agent_id = e2e_env.agent_id

    # The failure mode needs the fetch in flight while the user keeps
    # scrolling — delay only the scroll-up paging requests (they carry
    # `before=`; the initial timeline load must stay fast).
    before_requests: list[str] = []
    page.route(
        re.compile(r"/api/agents/\d+/timeline\?.*before="),
        lambda route: (
            before_requests.append(route.request.url),
            time.sleep(2.0),
            route.continue_(),
        ),
    )

    # The paging precondition below counts mounted [data-item-id] nodes; the
    # default details level "none" (user ruling 2026-09-17) collapses runs,
    # unmounting their members. Pin the expanded rendering the scenario
    # expects.
    pin_expand_runs_all(e2e_env.gateway_url)

    page.goto(e2e_env.agent_url)
    page.wait_for_selector('[data-testid="sse-ready"]', state="attached", timeout=10_000)

    # The scripted scenario runs when an inbound arrives — drive it, then wait
    # until the final reply is committed in the timeline (the whole script
    # ran; IDLING alone is racy — the agent idles between turns too).
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

    # Reload AFTER the scenario ran: the SSE-folded store holds every item
    # (hasMoreOlder=false — nothing older exists client-side), but a fresh
    # GET returns the 50-item tail window with has_more=true — the state the
    # user hits when opening an agent with a long history.
    page.reload()
    page.wait_for_selector('[data-testid="sse-ready"]', state="attached", timeout=10_000)

    # The timeline now holds ~60 items — past the 50-item tail window, so
    # scroll-up paging has a previous page (has_more=true).
    n = 0
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        n = page.evaluate("document.querySelectorAll('[data-item-id]').length")
        if n > 50:
            break
        page.wait_for_timeout(300)
    assert n >= 50, f"scenario timeline too short to page: {n} items"

    init = page.evaluate(_SAMPLER_JS)
    assert init.get("ok"), init

    rounds = _run_rounds(page, before_requests, init["n"], agent_id, e2e_env.gateway_url)
    assert rounds >= 2, f"expected multiple load-older rounds, got {rounds}"
