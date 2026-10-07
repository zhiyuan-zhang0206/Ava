"""Im bridge core cases: outbox replay delivers and clears."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from services.entrypoints.im_bridge import state as state_mod
from services.entrypoints.im_bridge.cursor_store import PushWatermark
from services.entrypoints.im_bridge.tests.test_im_bridge_core import (
    FakeGateway,
    FakePlainAdapter,
    FakeTypingAdapter,
    _core,
)
from services.entrypoints.im_bridge.types import ChatState, InboundMessage


def test_outbox_replay_delivers_and_clears(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """Regression #1032: after the gateway recovers, the replay drains the
    outboxed message with its persisted Idempotency-Key (AtLeastOnce) and
    clears the file. The key must be replayed unchanged so the gateway dedups
    a lost-response retry server-side."""
    monkeypatch.setenv("AVA_HOME", str(tmp_path))
    gateway = FakeGateway(send_failures=1)
    core = _core(gateway)
    adapter = FakeTypingAdapter()
    core.register(adapter)
    state = core._get_or_create_state("telegram", "12345")
    state.current_agent_id = 405
    msg = InboundMessage(channel="telegram", chat_id="12345", text="hello")

    async def scenario() -> None:
        await core.handle_inbound(msg)  # first send fails → outboxed
        entry = state_mod._load_outbox()[0]
        await core._replay_outbox_once()  # gateway back — drains
        assert gateway.sent == [(405, "hello", "user")]
        assert gateway.sent_keys == [entry.idempotency_key]
        assert state_mod._load_outbox() == []

    asyncio.run(scenario())


@pytest.mark.parametrize("ch", ("\u0085", "\u2028", "\u2029"), ids=("U+0085", "U+2028", "U+2029"))
def test_outbox_round_trip_keeps_unicode_line_separators(
    ch: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Regression (#3880, sibling of #1032): a queued message whose text
    carries U+0085 / U+2028 / U+2029 must survive the outbox round-trip.
    splitlines() split the JSONL line there, the reload skipped the entry
    ("skipping malformed outbox line"), and the next full rewrite erased it
    from disk — the drop #1032 exists to prevent."""
    monkeypatch.setenv("AVA_HOME", str(tmp_path))
    gateway = FakeGateway(send_failures=10)
    core = _core(gateway)
    adapter = FakeTypingAdapter()
    core.register(adapter)
    state = core._get_or_create_state("telegram", "12345")
    state.current_agent_id = 405
    text = f"a{ch}b"
    msg = InboundMessage(channel="telegram", chat_id="12345", text=text)

    async def scenario() -> None:
        await core.handle_inbound(msg)  # enqueue fails → outboxed
        outbox = state_mod._load_outbox()
        assert len(outbox) == 1
        assert outbox[0].text == text

    asyncio.run(scenario())


def test_push_snapshot_watermark_is_per_chat() -> None:
    """Regression (audit round 2, P1): the watermark used to be keyed by
    agent alone, so two chats switched to the same agent clobbered each
    other — the second chat's snapshot advanced the shared watermark past
    what the first chat had pushed, and the first chat permanently missed
    those items. The key must be (channel, chat_id, agent_id)."""
    gateway = FakeGateway()
    core = _core(gateway)
    adapter = FakeTypingAdapter()
    plain = FakePlainAdapter()
    core.register(adapter)
    core.register(plain)
    state_a = ChatState("telegram", "12345")
    state_a.current_agent_id = 405
    state_b = ChatState("weixin", "wx123")
    state_b.current_agent_id = 405

    def snapshot(*item_ids: str) -> dict[str, Any]:
        return {
            "items": [{"item_id": i, "kind": "agent_chat", "payload": f"p{i}"} for i in item_ids]
        }

    async def scenario() -> None:
        # Both chats receive the same snapshot (same agent); each must push
        # every item — a shared watermark would make the second chat stale.
        await core._push_snapshot(("telegram", "12345"), state_a, snapshot("1.1", "2.1"))
        await core._push_snapshot(("weixin", "wx123"), state_b, snapshot("1.1", "2.1"))
        assert adapter.sent == [
            ("12345", "[Ava #405] p1.1"),
            ("12345", "[Ava #405] p2.1"),
        ]
        assert plain.sent == [
            ("wx123", "[Ava #405] p1.1"),
            ("wx123", "[Ava #405] p2.1"),
        ]
        # A new item for chat A alone must not be suppressed by chat B's
        # watermark (and vice versa).
        await core._push_snapshot(("telegram", "12345"), state_a, snapshot("3.1"))
        await core._push_snapshot(("weixin", "wx123"), state_b, snapshot("4.1"))
        assert adapter.sent[-1:] == [("12345", "[Ava #405] p3.1")]
        assert plain.sent[-1:] == [("wx123", "[Ava #405] p4.1")]
        assert core._last_pushed[("telegram", "12345", 405)] == PushWatermark(None, "3.1")
        assert core._last_pushed[("weixin", "wx123", 405)] == PushWatermark(None, "4.1")

    asyncio.run(scenario())
