"""Three visible pages in one browser profile share three gateway SSE sockets."""

from __future__ import annotations

from collections import defaultdict
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, Request

from tests.e2e._env import E2EEnv
from tests.shared.poll_until import poll_until

_CHANNEL_PATHS = {
    "/api/system": "system",
    "/api/alerts/stream": "alerts",
    "/api/system/all": "systemAll",
}

_PAGE_INIT = """(() => {
  let visible = true;
  Object.defineProperty(document, "visibilityState", {
    configurable: true, get: () => visible ? "visible" : "hidden",
  });
  window.__setTestVisible = (next) => {
    visible = next;
    document.dispatchEvent(new Event("visibilitychange"));
  };
  const NativeEventSource = window.EventSource;
  const active = new Set();
  window.__activeTestSse = () => active.size;
  window.EventSource = class extends NativeEventSource {
    constructor(url, init) {
      super(url, init);
      active.add(this);
    }
    close() {
      super.close();
      active.delete(this);
    }
  };
})()"""


def _active_count(page: Page) -> int:
    return page.evaluate("() => window.__activeTestSse()")


def _open_pages(
    e2e_env: E2EEnv,
    requests: dict[Page, list[str]],
    *,
    without_locks: bool = False,
    count: int = 3,
) -> list[Page]:
    context = e2e_env.page.context
    pages = [e2e_env.page, *(context.new_page() for _ in range(count - 1))]

    def track(page: Page) -> None:
        def on_request(request: Request) -> None:
            channel = _CHANNEL_PATHS.get(urlparse(request.url).path)
            if channel is not None:
                requests[page].append(channel)

        page.on("request", on_request)
        page.add_init_script(_PAGE_INIT)
        if without_locks:
            page.add_init_script(
                'Object.defineProperty(Navigator.prototype, "locks", '
                "{ configurable: true, get: () => undefined });"
            )

    for page in pages:
        track(page)
        page.goto(e2e_env.agent_url)
        page.wait_for_selector('[data-testid="sse-ready"]', state="attached", timeout=20_000)
    return pages


def _wait_counts(pages: list[Page], expected: list[int], what: str, *, sorted_counts: bool) -> None:
    def matches() -> tuple[bool, object]:
        for page in pages:
            page.evaluate("() => 1")
        counts = [_active_count(page) for page in pages]
        observed = sorted(counts) if sorted_counts else counts
        return observed == expected, counts

    poll_until(matches, timeout=15.0, interval=0.1, what=what)


def _assert_requests(pages: list[Page], requests: dict[Page, list[str]]) -> None:
    for channel in _CHANNEL_PATHS.values():
        assert sum(requests[page].count(channel) for page in pages) == 1, (
            channel,
            {index: requests[page] for index, page in enumerate(pages)},
        )


@pytest.mark.scenario("tests.e2e.fakes.scenarios.lifecycle_terminate:build")
def test_three_pages_share_sse_and_promote_on_close_and_hide(e2e_env: E2EEnv) -> None:
    requests: dict[Page, list[str]] = defaultdict(list)
    pages = _open_pages(e2e_env, requests)
    _wait_counts(pages, [0, 0, 3], "one SSE owner across three pages", sorted_counts=True)
    _assert_requests(pages, requests)

    leader = next(page for page in pages if _active_count(page) == 3)
    survivors = [page for page in pages if page is not leader]
    leader.close()
    _wait_counts(survivors, [0, 3], "SSE promotion after close", sorted_counts=True)
    promoted = next(page for page in survivors if _active_count(page) == 3)
    waiting = next(page for page in survivors if page is not promoted)
    _assert_requests([promoted], requests)
    for page in survivors:
        page.wait_for_selector('[data-testid="sse-ready"]', state="attached", timeout=10_000)

    promoted.evaluate("() => window.__setTestVisible(false)")
    _wait_counts([promoted, waiting], [0, 3], "SSE promotion after hide", sorted_counts=False)
    _assert_requests([waiting], requests)

    promoted.evaluate("() => window.__setTestVisible(true)")
    promoted.wait_for_selector('[data-testid="sse-ready"]', state="attached", timeout=10_000)
    assert (_active_count(promoted), _active_count(waiting)) == (0, 3)


@pytest.mark.scenario("tests.e2e.fakes.scenarios.lifecycle_terminate:build")
def test_without_web_locks_keeps_per_page_streams_and_navigation(e2e_env: E2EEnv) -> None:
    requests: dict[Page, list[str]] = defaultdict(list)
    # Two pages fill Chromium's six HTTP/1.1 SSE slots in fallback mode.
    pages = _open_pages(e2e_env, requests, without_locks=True, count=2)
    _wait_counts(pages, [3, 3], "two independent fallback stream sets", sorted_counts=False)
    for page in pages:
        assert requests[page].count("system") == 1
        assert requests[page].count("alerts") == 1
        assert requests[page].count("systemAll") == 1

    navigated = pages[0]
    navigated.get_by_role("button", name="Fleet").first.click()
    navigated.wait_for_url("**/fleet?agent_id=*")
    navigated.wait_for_selector('[data-testid="sse-ready"]', state="attached", timeout=10_000)
    _wait_counts(
        pages, [2, 3], "fallback global streams survive route navigation", sorted_counts=False
    )

    navigated.go_back()
    navigated.wait_for_selector('[data-testid="sse-ready"]', state="attached", timeout=10_000)
    _wait_counts(pages, [3, 3], "fallback detail stream returns after back", sorted_counts=False)
