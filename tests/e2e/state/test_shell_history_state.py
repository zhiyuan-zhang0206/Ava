from __future__ import annotations

import re
import time
from typing import TypedDict, cast

import httpx
import pytest
from playwright.sync_api import Page

from tests.e2e._settings import pin_expand_runs_all
from tests.e2e.fixture_environment import E2EEnv


class _TimelineState(TypedDict):
    st: int | None
    max: int | None
    n: int


class _PaneState(TypedDict):
    st: int | None
    max: int | None
    len: int


class _Frame(TypedDict):
    t: int
    p: str
    n: int
    tls: int | None
    tlm: int | None
    txt: int
    pre: int | None
    ss: int | None
    sm: int | None
    inv: bool
    mt: int
    ph: bool
    home: bool
    pane: bool


TIMELINE_SCROLL_TOP = 700
PANE_SCROLL_TOP = 400

_FRAME_SAMPLER_JS = """
() => {
  window.__hf = { frames: [] };
  const snap = () => {
    const items = document.querySelectorAll('[data-item-id]');
    let tl = null;
    for (const v of document.querySelectorAll('[data-slot="scroll-area-viewport"]')) {
      if (v.querySelectorAll('[data-item-id]').length > 0) { tl = v; break; }
    }
    const pane = document.querySelector('[data-testid="shell-pane"]');
    const pre = pane ? pane.querySelector('pre') : null;
    const main = document.querySelector('#main-content');
    const f = {
      t: Math.round(performance.now()),
      p: location.pathname,
      n: items.length,
      tls: tl ? Math.round(tl.scrollTop) : null,
      tlm: tl ? Math.round(tl.scrollHeight - tl.clientHeight) : null,
      txt: tl ? tl.textContent.length : 0,
      home: !!document.querySelector('[data-testid="timeline-surface"]'),
      pane: !!pane,
      pre: pre ? pre.textContent.length : null,
      ss: pane ? Math.round(pane.scrollTop) : null,
      sm: pane ? Math.round(pane.scrollHeight - pane.clientHeight) : null,
      inv: main ? main.innerText.includes('Invalid agent or session id') : false,
      mt: main ? main.innerText.length : 0,
      ph: !!document.querySelector('[data-testid="home-layout-placeholder"]'),
    };
    const fr = window.__hf.frames;
    if (fr.length < 8000) fr.push(f);
  };
  const loop = () => { snap(); requestAnimationFrame(loop); };
  requestAnimationFrame(loop);
  snap();
  return true;
}
"""

_TIMELINE_STATE_JS = """
() => {
  for (const v of document.querySelectorAll('[data-slot="scroll-area-viewport"]')) {
    if (v.querySelectorAll('[data-item-id]').length > 0) {
      return {
        st: Math.round(v.scrollTop),
        max: Math.round(v.scrollHeight - v.clientHeight),
        n: v.querySelectorAll('[data-item-id]').length,
      };
    }
  }
  return { st: null, max: null, n: 0 };
}
"""

_PANE_STATE_JS = """
() => {
  const pane = document.querySelector('[data-testid="shell-pane"]');
  const pre = pane ? pane.querySelector('pre') : null;
  if (!pane || !pre) return { st: null, max: null, len: 0 };
  return {
    st: Math.round(pane.scrollTop),
    max: Math.round(pane.scrollHeight - pane.clientHeight),
    len: pre.textContent.length,
  };
}
"""


def _frames(page: Page) -> list[_Frame]:
    return cast("list[_Frame]", page.evaluate("window.__hf ? window.__hf.frames : []"))


def _frames_tail(page: Page, count: int = 10) -> list[_Frame]:
    return _frames(page)[-count:]


def _timeline_state(page: Page) -> _TimelineState:
    return cast("_TimelineState", page.evaluate(_TIMELINE_STATE_JS))


def _pane_state(page: Page) -> _PaneState:
    return cast("_PaneState", page.evaluate(_PANE_STATE_JS))


def _settle(page: Page, read_js: str, timeout_s: float = 20.0) -> None:
    deadline = time.monotonic() + timeout_s
    prev: object = None
    while time.monotonic() < deadline:
        cur: object = page.evaluate(read_js)
        if (
            prev is not None
            and cur == prev
            and isinstance(cur, dict)
            and cast("dict[str, object]", cur).get("st") is not None
        ):
            return
        prev = cur
        page.wait_for_timeout(500)


def _pin_page_settings(gateway_url: str) -> None:
    pin_expand_runs_all(gateway_url)
    resp = httpx.put(
        f"{gateway_url}/api/settings/display.inspector_open",
        json={"value": True},
        timeout=30.0,
    )
    resp.raise_for_status()


def _run_scenario(page: Page, agent_url: str, gateway_url: str, agent_id: int) -> int:
    page.goto(agent_url)
    page.wait_for_selector('[data-testid="sse-ready"]', state="attached", timeout=15_000)
    page.fill('[data-testid="composer-input"]', "run the script")
    page.click('[data-testid="composer-send"]')

    sid: int | None = None
    deadline = time.monotonic() + 150.0
    while time.monotonic() < deadline and sid is None:
        items = httpx.get(
            f"{gateway_url}/api/agents/{agent_id}/timeline?limit=1000", timeout=60.0
        ).json()["items"]
        for it in items:
            m = re.search(r"SHELL_SID=(\d+)", it.get("payload") or "")
            if m:
                sid = int(m.group(1))
                break
        if sid is None:
            time.sleep(0.5)
    assert sid is not None, "the scenario never committed a SHELL_SID marker"
    return sid


def _place_timeline_reader(page: Page) -> None:
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        state = _timeline_state(page)
        if state["max"] is not None and state["max"] > TIMELINE_SCROLL_TOP + 200:
            break
        page.wait_for_timeout(300)
    page.evaluate(
        """
        (top) => {
          for (const v of document.querySelectorAll('[data-slot="scroll-area-viewport"]')) {
            if (v.querySelectorAll('[data-item-id]').length > 0) { v.scrollTop = top; return; }
          }
        }
        """,
        TIMELINE_SCROLL_TOP,
    )
    page.wait_for_timeout(400)
    state = _timeline_state(page)
    assert state["st"] == TIMELINE_SCROLL_TOP, (
        f"the timeline did not take the reading position: {state}"
    )


def _open_shell_row(page: Page, shell_link: str) -> None:
    page.click(shell_link)
    page.wait_for_url(re.compile(r"/shell/"))
    page.wait_for_selector('[data-testid="shell-pane"]', timeout=15_000)
    deadline = time.monotonic() + 30.0
    pane = _pane_state(page)
    while time.monotonic() < deadline:
        pane = _pane_state(page)
        if pane["len"] > 100 and pane["max"] is not None and pane["max"] > PANE_SCROLL_TOP + 200:
            return
        page.wait_for_timeout(200)
    raise AssertionError(f"the shell capture never rendered long enough: {pane}")


def _place_pane_reader(page: Page) -> None:
    page.evaluate(
        "(top) => { document.querySelector('[data-testid=\"shell-pane\"]').scrollTop = top; }",
        PANE_SCROLL_TOP,
    )
    page.wait_for_timeout(500)
    pane = _pane_state(page)
    assert pane["st"] == PANE_SCROLL_TOP, (
        f"the terminal pane did not take the reading position: {pane}"
    )


def _is_home_frame(frame: _Frame) -> bool:
    return frame["p"] == "/" and frame["home"]


def _is_shell_frame(frame: _Frame) -> bool:
    return frame["p"].startswith("/shell/") and frame["pane"]


def _assert_no_placeholder_frame(page: Page, frames: list[_Frame]) -> None:
    placeholders = [f for f in frames if f["ph"]]
    assert not placeholders, (
        "the layout placeholder painted "
        f"{len(placeholders)} frame(s): {placeholders[:4]}; frames tail={_frames_tail(page)}"
    )


def _assert_home_frames_show_content(back_frames: list[_Frame]) -> None:
    blank_home = [f for f in back_frames if f["n"] == 0 or f["txt"] == 0]
    if not blank_home:
        return
    first = back_frames.index(blank_home[0])
    window = back_frames[max(0, first - 6) : first + 12]
    raise AssertionError(
        f"the returned home page painted {len(blank_home)} frame(s) without "
        f"timeline content: window={window} all={blank_home}"
    )


def _assert_shell_frames_show_capture(page: Page, forward_frames: list[_Frame]) -> None:
    bad_shell = [f for f in forward_frames if f["pre"] is None or f["pre"] == 0]
    assert not bad_shell, (
        f"the returned shell page painted {len(bad_shell)} frame(s) without the "
        f"capture (invalid-params/loading state): {bad_shell}; "
        f"frames tail={_frames_tail(page)}"
    )


def _assert_returned_pages_painted_content(
    page: Page, back_marker: int, forward_marker: int
) -> None:
    frames = _frames(page)
    back_frames = [f for f in frames[back_marker:forward_marker] if _is_home_frame(f)]
    forward_frames = [f for f in frames[forward_marker:] if _is_shell_frame(f)]
    _assert_no_placeholder_frame(page, frames)
    _assert_home_frames_show_content(back_frames)
    _assert_shell_frames_show_capture(page, forward_frames)


def _assert_poll_and_refresh_alive(page: Page, captures: list[float]) -> None:
    pre_len_before = page.evaluate(
        "() => document.querySelector('[data-testid=\"shell-pane\"]').textContent.length"
    )
    deadline = time.monotonic() + 10.0
    polled = False
    while time.monotonic() < deadline and not polled:
        page.wait_for_timeout(400)
        polled = (
            page.evaluate(
                "() => document.querySelector('[data-testid=\"shell-pane\"]').textContent.length"
            )
            > pre_len_before
        )
    assert polled, (
        "the 3s poll stopped updating the pane after the round trip "
        f"(content stayed {pre_len_before} chars, {len(captures)} captures so far)"
    )

    refresh = page.locator('button[aria-label="Refresh shell output"]')
    refresh.wait_for(state="visible", timeout=5_000)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and refresh.is_disabled():
        page.wait_for_timeout(200)
    n_before_refresh = len(captures)
    refresh.click()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and len(captures) == n_before_refresh:
        page.wait_for_timeout(200)
    assert len(captures) > n_before_refresh, "the manual refresh did not fetch after the round trip"


@pytest.mark.scenario("tests.e2e.fakes.scenarios.shell_history:build")
def test_back_forward_keeps_scroll_position_without_blank_frames(e2e_env: E2EEnv) -> None:
    page = e2e_env.page
    agent_id = e2e_env.agent_id
    gateway_url = e2e_env.gateway_url

    _pin_page_settings(gateway_url)

    captures: list[float] = []
    page.route(
        re.compile(r"/api/agents/\d+/shell/\d+"),
        lambda route: (captures.append(time.monotonic()), route.continue_()),
    )

    sid = _run_scenario(page, e2e_env.agent_url, gateway_url, agent_id)
    shell_link = f'a[href="/shell/{agent_id}/{sid}"]'
    page.wait_for_selector(shell_link, timeout=60_000)

    _place_timeline_reader(page)

    page.evaluate(_FRAME_SAMPLER_JS)

    _open_shell_row(page, shell_link)
    _place_pane_reader(page)

    back_marker = len(_frames(page))
    page.go_back()
    page.wait_for_url(re.compile(r"/\?agent_id="))
    _settle(page, _TIMELINE_STATE_JS)
    home_after = _timeline_state(page)
    home_st = home_after["st"]
    assert home_st is not None and abs(home_st - TIMELINE_SCROLL_TOP) <= 2, (
        "back must restore the timeline reading position "
        f"(want {TIMELINE_SCROLL_TOP}, got {home_after}); "
        f"frames tail={_frames_tail(page)}"
    )

    forward_marker = len(_frames(page))
    page.go_forward()
    page.wait_for_url(re.compile(r"/shell/"))
    _settle(page, _PANE_STATE_JS)
    shell_after = _pane_state(page)
    shell_st = shell_after["st"]
    assert shell_st is not None and abs(shell_st - PANE_SCROLL_TOP) <= 2, (
        "forward must restore the terminal pane reading position "
        f"(want {PANE_SCROLL_TOP}, got {shell_after}); "
        f"frames tail={_frames_tail(page)}"
    )

    _assert_returned_pages_painted_content(page, back_marker, forward_marker)
    _assert_poll_and_refresh_alive(page, captures)
