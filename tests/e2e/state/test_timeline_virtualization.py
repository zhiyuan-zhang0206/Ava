"""Deep-history timeline: real-browser DOM bound and reproducible frame sample."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol, cast
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import Route

from tests.e2e._env import E2EEnv
from tests.e2e._settings import pin_expand_runs_all

ITEM_COUNT = 1200


class _CDPSession(Protocol):
    def send(self, method: str) -> dict[str, object]: ...


def _serve_deep_history(
    route: Route,
    tail: Mapping[str, object],
    older: Mapping[str, object],
    older_requests: list[str],
) -> None:
    url = urlparse(route.request.url)
    if url.path.endswith("/timeline"):
        if "before" in parse_qs(url.query):
            older_requests.append(route.request.url)
            route.fulfill(json=older)
        else:
            route.fulfill(json=tail)
        return
    response = route.fetch()
    snapshot = response.json()
    snapshot["timeline"] = tail
    route.fulfill(response=response, json=snapshot)


def _assert_bounded_sample(sample: dict[str, float | int], kind: str) -> None:
    assert sample["mountedRows"] < 80, sample
    assert sample["maxMountedRows"] < 80, sample
    assert sample["spacerCount"] >= 1, sample
    # The A/B frame gate uses identical primary-row content in every variant.
    # Expanded turns also assert the DOM and prepend invariants above.
    if kind == "agent_chat":
        assert sample["frameP95Ms"] < 33, sample


@pytest.mark.scenario("tests.e2e.fakes.scenarios.message_flow:build")
@pytest.mark.parametrize("kind", ["agent_chat", "agent_reasoning"])
def test_parked_deep_history_mounts_a_bounded_window(e2e_env: E2EEnv, kind: str) -> None:
    page = e2e_env.page
    items = [
        {
            "item_id": f"{index}.0",
            "kind": kind,
            "payload": f"Deep history reply {index}",
            "source": None,
            "created_at": "2026-09-25T00:00:00Z",
            "inbound_id": None,
            "show_timestamp": False,
        }
        for index in range(1, ITEM_COUNT + 1)
    ]
    tail = {"items": items[-50:], "msg_count": ITEM_COUNT, "has_more": True}
    older = {"items": items[:-50], "msg_count": ITEM_COUNT, "has_more": False}
    older_requests: list[str] = []

    page.route(
        re.compile(r"/api/agents/\d+/(timeline|conversation-snapshot)"),
        lambda route: _serve_deep_history(route, tail, older, older_requests),
    )
    if kind == "agent_reasoning":
        pin_expand_runs_all(e2e_env.gateway_url)
    page.goto(e2e_env.agent_url)
    page.wait_for_selector('[data-testid="sse-ready"]', state="attached", timeout=15_000)
    page.get_by_text("Deep history reply 1200", exact=False).wait_for(timeout=30_000)
    anchor = page.evaluate("""() => {
      const viewport = document.querySelector('[role=log]')?.closest('[data-slot=scroll-area-viewport]');
      if (!viewport) throw new Error('timeline viewport missing');
      viewport.scrollTop = Math.max(0, viewport.scrollHeight - viewport.clientHeight - 400);
      viewport.dispatchEvent(new Event('scroll'));
      viewport.scrollTop = 0;
      viewport.dispatchEvent(new Event('scroll'));
      const viewportTop = viewport.getBoundingClientRect().top;
      const row = [...viewport.querySelectorAll('.timeline-item')]
        .find((node) => node.getBoundingClientRect().bottom > viewportTop);
      if (!row) throw new Error('no visible reading anchor');
      return { id: row.dataset.itemId, viewportOffsetPx: row.getBoundingClientRect().top - viewportTop };
    }""")
    page.wait_for_timeout(1500)
    assert older_requests, "a parked reader did not request older history"
    assert (
        page.evaluate(
            "document.querySelector('[role=log]')?.closest('[data-slot=scroll-area-viewport]')?.scrollHeight"
        )
        > 50_000
    ), "deep history did not land"
    landed_offset = page.evaluate(
        """id => {
      const viewport = document.querySelector('[role=log]')?.closest('[data-slot=scroll-area-viewport]');
      const row = viewport?.querySelector(`.timeline-item[data-item-id="${CSS.escape(id)}"]`);
      return viewport && row ? row.getBoundingClientRect().top - viewport.getBoundingClientRect().top : null;
    }""",
        anchor["id"],
    )
    assert landed_offset is not None, (
        "the reading anchor was released during prepend",
        anchor,
        page.evaluate("""() => ({
          rows: [...document.querySelectorAll('.timeline-item')].map(n => n.dataset.itemId),
          spacers: [...document.querySelectorAll('[data-timeline-spacer]')].map(n => [n.dataset.timelineSpacer, n.clientHeight]),
          scrollTop: document.querySelector('[role=log]')?.closest('[data-slot=scroll-area-viewport]')?.scrollTop,
        })"""),
    )
    assert abs(landed_offset - anchor["viewportOffsetPx"]) < 3, (anchor, landed_offset)
    sample = page.evaluate("""async () => {
      const viewport = document.querySelector('[role=log]')?.closest('[data-slot=scroll-area-viewport]');
      if (!viewport) throw new Error('timeline viewport missing');
      const frames = [];
      let maxMountedRows = 0;
      await new Promise((resolve) => {
        let frame = 0;
        let previous = 0;
        function step(now) {
          if (previous && frame >= 20) frames.push(now - previous);
          previous = now;
          viewport.scrollTop = Math.min(viewport.scrollHeight - viewport.clientHeight, frame * 120);
          maxMountedRows = Math.max(maxMountedRows, viewport.querySelectorAll('.timeline-item').length);
          frame++;
          if (frame < 140) requestAnimationFrame(step);
          else resolve();
        }
        requestAnimationFrame(step);
      });
      frames.sort((a, b) => a - b);
      return {
        viewportHeight: viewport.clientHeight,
        totalItems: 1200,
        mountedRows: viewport.querySelectorAll('.timeline-item').length,
        maxMountedRows,
        attachedNodes: viewport.querySelectorAll('*').length,
        spacerCount: viewport.querySelectorAll('[data-timeline-spacer]').length,
        scrollHeight: viewport.scrollHeight,
        frameP95Ms: frames[Math.floor(frames.length * 0.95)],
        frameMaxMs: frames[frames.length - 1],
      };
    }""")
    cdp = cast(_CDPSession, page.context.new_cdp_session(page))
    cdp.send("Performance.enable")
    metrics = cast(list[dict[str, float | str]], cdp.send("Performance.getMetrics")["metrics"])
    sample["jsHeapUsedBytes"] = next(
        metric["value"] for metric in metrics if metric["name"] == "JSHeapUsedSize"
    )
    sample["prependAnchorDeltaPx"] = landed_offset - anchor["viewportOffsetPx"]
    if kind == "agent_chat" and (output := os.environ.get("AVA_TIMELINE_BENCH_OUT")):
        Path(output).write_text(json.dumps(sample, indent=2, sort_keys=True) + "\n")
    _assert_bounded_sample(sample, kind)
