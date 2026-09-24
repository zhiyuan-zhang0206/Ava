"""A painted compact transition keeps re-served history present in every frame."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from playwright.sync_api import Request

from shared.agents import AgentStatus
from tests.e2e._db import wait_for_status
from tests.e2e._env import E2EEnv
from tests.e2e._settings import pin_expand_runs_all
from tests.e2e.fakes.scenarios.compact_transition import (
    BETWEEN_REPLY,
    BETWEEN_TAIL,
    FIRST_NARRATION,
    FIRST_REPLY,
    FIRST_TAIL,
    SECOND_NARRATION,
)


@pytest.fixture
def transition_env(request: pytest.FixtureRequest) -> Iterator[E2EEnv]:
    # Package setup has already written the throwaway cluster's authoritative
    # .env. The gateway enforces that file over its inherited process env.
    env_path = Path(os.environ["AVA_HOME"]) / ".env"
    original = env_path.read_text()
    lines = [
        line
        for line in original.splitlines()
        if not line.startswith("AVA_TIMELINE_COMPACT_HISTORY=")
    ]
    env_path.write_text("\n".join([*lines, "AVA_TIMELINE_COMPACT_HISTORY=-1"]) + "\n")
    try:
        yield request.getfixturevalue("e2e_env")
    finally:
        env_path.write_text(original)


_FRAMES_JS = """
(() => {
  const root = document.querySelector('[role="log"]');
  if (!root) throw new Error('timeline missing');
  const frames = [];
  const state = { phase: 'idle', frames, active: true };
  const viewport = root.closest('[data-slot="scroll-area-viewport"]');
  if (!viewport) throw new Error('timeline viewport missing');
  state.placeReply = (text) => {
    const row = [...root.querySelectorAll('[data-item-id]')].reverse()
      .find((node) => node.textContent?.includes(text));
    if (!row) throw new Error('reply missing: ' + text);
    const top = row.getBoundingClientRect().top - viewport.getBoundingClientRect().top + viewport.scrollTop;
    viewport.scrollTop = Math.max(0, top - viewport.clientHeight / 2);
    viewport.dispatchEvent(new Event('scroll'));
    return { top: row.getBoundingClientRect().top, scrollTop: viewport.scrollTop,
      distanceFromBottom: viewport.scrollHeight - viewport.clientHeight - viewport.scrollTop };
  };
  state.replyTop = (text) => {
    const row = [...root.querySelectorAll('[data-item-id]')].reverse()
      .find((node) => node.textContent?.includes(text));
    return row?.getBoundingClientRect().top ?? null;
  };
  function sample() {
    const text = root.textContent ?? '';
    frames.push({ phase: state.phase,
      first: text.includes('TRANSITION_FIRST_REPLY'),
      between: text.includes('TRANSITION_BETWEEN_REPLY'),
      firstTop: state.replyTop('TRANSITION_FIRST_REPLY'),
      betweenTop: state.replyTop('TRANSITION_BETWEEN_REPLY'),
      firstNarration: text.includes('TRANSITION_FIRST_NARRATION'),
      secondNarration: text.includes('TRANSITION_SECOND_NARRATION'),
      ranks: [...root.querySelectorAll('[data-testid="compact-history-divider"]')]
        .map((node) => node.getAttribute('data-segment-rank')) });
    if (state.active) requestAnimationFrame(sample);
  }
  window.__compactTransitionFrames = state;
  requestAnimationFrame(sample);
})();
"""


def _send(env: E2EEnv, message: str, reply: str) -> None:
    env.page.fill('[data-testid="composer-input"]', message)
    env.page.click('[data-testid="composer-send"]')
    env.page.get_by_text(reply, exact=False).wait_for(timeout=30_000)
    wait_for_status(env.agent_id, AgentStatus.IDLING.value)


def _compact(env: E2EEnv, narration: str) -> None:
    response = httpx.post(f"{env.gateway_url}/api/agents/{env.agent_id}/compact", timeout=30.0)
    response.raise_for_status()
    assert response.json()["status"] == "enqueued"
    env.page.get_by_text(narration, exact=False).wait_for(timeout=45_000)
    wait_for_status(env.agent_id, AgentStatus.IDLING.value)


def _prepare_history_view(e2e_env: E2EEnv, retention: int) -> list[str]:
    setting = httpx.put(
        f"{e2e_env.gateway_url}/api/settings/display.compact_history_sessions",
        json={"value": retention},
        timeout=30.0,
    )
    setting.raise_for_status()
    config = httpx.get(f"{e2e_env.gateway_url}/api/config", timeout=30.0)
    config.raise_for_status()
    depth = next(
        field for field in config.json()["fields"] if field["name"] == "timeline_compact_history"
    )
    assert depth["current_value"] == -1

    before_requests: list[str] = []

    def record_history_request(request: Request) -> None:
        url = request.url
        parsed = urlparse(url)
        if parsed.path.endswith("/timeline") and "before" in parse_qs(parsed.query):
            before_requests.append(url)

    e2e_env.page.on("request", record_history_request)
    pin_expand_runs_all(e2e_env.gateway_url)
    e2e_env.page.goto(e2e_env.agent_url)
    e2e_env.page.wait_for_selector('[data-testid="sse-ready"]', state="attached", timeout=15_000)
    return before_requests


def _place_nonsticky_reply(env: E2EEnv, retention: int, reply: str) -> dict[str, Any] | None:
    if retention == 0:
        return None
    position: dict[str, Any] = env.page.evaluate(
        "reply => window.__compactTransitionFrames.placeReply(reply)", reply
    )
    assert position["distanceFromBottom"] > 200, position
    return position


def _assert_settled_anchor(
    env: E2EEnv, retention: int, ranks: str, reply: str, position: dict[str, Any] | None
) -> None:
    if retention == 0:
        return
    env.page.wait_for_function(
        "expected => [...document.querySelectorAll('[data-testid=compact-history-divider]')]"
        ".map(node => node.dataset.segmentRank).join(',') === expected && "
        "!document.querySelector('[data-timeline-source=buffer]')",
        arg=ranks,
        timeout=30_000,
    )
    top = env.page.evaluate("reply => window.__compactTransitionFrames.replyTop(reply)", reply)
    assert position is not None and top is not None
    assert abs(top - position["top"]) < 4, (position, top)


def _assert_phase_frames(
    frames: list[dict[str, Any]],
    phase: str,
    narration_key: str,
    visible_keys: tuple[str, ...],
    top_key: str,
    position: dict[str, Any],
) -> None:
    sampled = [frame for frame in frames if frame["phase"] == phase]
    assert any(frame[narration_key] for frame in sampled), sampled
    for frame in sampled:
        assert all(frame[key] for key in visible_keys), frame
        assert abs(frame[top_key] - position["top"]) < 4, frame


def _assert_retained_frames(
    frames: list[dict[str, Any]],
    first_position: dict[str, Any] | None,
    between_position: dict[str, Any] | None,
    before_requests: list[str],
) -> None:
    assert first_position is not None and between_position is not None
    _assert_phase_frames(frames, "first", "firstNarration", ("first",), "firstTop", first_position)
    _assert_phase_frames(
        frames, "second", "secondNarration", ("first", "between"), "betweenTop", between_position
    )
    assert frames[-1]["ranks"] == ["2", "1", "0"]
    assert before_requests


@pytest.mark.parametrize("retention", [-1, 0], ids=["all", "zero"])
@pytest.mark.scenario("tests.e2e.fakes.scenarios.compact_transition:build")
def test_two_compact_painted_history_transition(transition_env: E2EEnv, retention: int) -> None:
    env = transition_env
    before_requests = _prepare_history_view(env, retention)
    _send(env, "first message", FIRST_REPLY)
    _send(env, "first tail message", FIRST_TAIL)
    env.page.evaluate(_FRAMES_JS)
    first_position = _place_nonsticky_reply(env, retention, FIRST_REPLY)
    env.page.evaluate("window.__compactTransitionFrames.phase = 'first'")
    _compact(env, FIRST_NARRATION)
    _assert_settled_anchor(env, retention, "1,0", FIRST_REPLY, first_position)
    env.page.evaluate("window.__compactTransitionFrames.phase = 'between'")
    _send(env, "between message", BETWEEN_REPLY)
    _send(env, "between tail message", BETWEEN_TAIL)
    between_position = _place_nonsticky_reply(env, retention, BETWEEN_REPLY)
    env.page.evaluate("window.__compactTransitionFrames.phase = 'second'")
    _compact(env, SECOND_NARRATION)
    _assert_settled_anchor(env, retention, "2,1,0", BETWEEN_REPLY, between_position)
    frames: list[dict[str, Any]] = env.page.evaluate(
        "() => { const state = window.__compactTransitionFrames; state.active = false; return state.frames; }"
    )
    if retention == -1:
        _assert_retained_frames(frames, first_position, between_position, before_requests)
    else:
        assert before_requests == []
        assert not env.page.get_by_text(FIRST_REPLY, exact=False).count()
