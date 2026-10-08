"""Im bridge core cases: outbox replay delivers and clears."""

from __future__ import annotations

from typing import Any

import pytest

from services.entrypoints.im_bridge import state as state_mod
from services.entrypoints.im_bridge.cursor_store import PushWatermark
from services.entrypoints.im_bridge.tests.task_scope import owned_tasks
from services.entrypoints.im_bridge.tests.test_im_bridge_core import (
    FakeGateway,
    FakePlainAdapter,
    FakeTypingAdapter,
    _core,
)
from services.entrypoints.im_bridge.types import ChatState, InboundMessage


async def test_outbox_replay_delivers_and_clears(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Regression #1032: after the gateway recovers, the replay drains the
    outboxed message with its persisted Idempotency-Key (AtLeastOnce) and
    clears the file. The key must be replayed unchanged so the gateway dedups
    a lost-response retry server-side."""
    async with owned_tasks() as _owned_tasks:
        monkeypatch.setenv("AVA_HOME", str(tmp_path))
        gateway = FakeGateway(send_failures=1)
        core = _core(gateway, tasks=_owned_tasks)
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

        await scenario()


@pytest.mark.parametrize("ch", ("\u0085", "\u2028", "\u2029"), ids=("U+0085", "U+2028", "U+2029"))
async def test_outbox_round_trip_keeps_unicode_line_separators(
    ch: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Regression (#3880, sibling of #1032): a queued message whose text
    carries U+0085 / U+2028 / U+2029 must survive the outbox round-trip.
    splitlines() split the JSONL line there, the reload skipped the entry
    ("skipping malformed outbox line"), and the next full rewrite erased it
    from disk — the drop #1032 exists to prevent."""
    async with owned_tasks() as _owned_tasks:
        monkeypatch.setenv("AVA_HOME", str(tmp_path))
        gateway = FakeGateway(send_failures=10)
        core = _core(gateway, tasks=_owned_tasks)
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

        await scenario()


async def test_push_snapshot_watermark_is_per_chat() -> None:
    """Regression (audit round 2, P1): the watermark used to be keyed by
    agent alone, so two chats switched to the same agent clobbered each
    other — the second chat's snapshot advanced the shared watermark past
    what the first chat had pushed, and the first chat permanently missed
    those items. The key must be (channel, chat_id, agent_id)."""
    async with owned_tasks() as _owned_tasks:
        gateway = FakeGateway()
        core = _core(gateway, tasks=_owned_tasks)
        adapter = FakeTypingAdapter()
        plain = FakePlainAdapter()
        core.register(adapter)
        core.register(plain)
        state_a = ChatState("telegram", "12345")
        state_a.current_agent_id = 405
        state_b = ChatState("weixin", "wx123")
        state_b.current_agent_id = 405

        def committed(*item_ids: str) -> None:
            gateway.timeline = [
                {
                    "item_id": item_id,
                    "kind": "agent_chat",
                    "payload": f"p{item_id}",
                    "source_message_id": "stored-" + item_id,
                    "source_block_idx": 0,
                }
                for item_id in item_ids
            ]

        async def scenario() -> None:
            committed("1.1", "2.1")
            await core._push_snapshot(("telegram", "12345"), state_a, {})
            await core._push_snapshot(("weixin", "wx123"), state_b, {})
            await core.outbound_worker.run_once()
            await core.outbound_worker.run_once()
            assert adapter.sent == [("12345", "[Ava #405] p1.1"), ("12345", "[Ava #405] p2.1")]
            assert plain.sent == [("wx123", "[Ava #405] p1.1"), ("wx123", "[Ava #405] p2.1")]
            committed("3.1")
            await core._push_snapshot(("telegram", "12345"), state_a, {})
            committed("4.1")
            await core._push_snapshot(("weixin", "wx123"), state_b, {})
            await core.outbound_worker.run_once()
            assert adapter.sent[-1:] == [("12345", "[Ava #405] p3.1")]
            assert plain.sent[-1:] == [("wx123", "[Ava #405] p4.1")]
            assert core._last_pushed[("telegram", "12345", 405)] == PushWatermark(None, "3.1")
            assert core._last_pushed[("weixin", "wx123", 405)] == PushWatermark(None, "4.1")

        await scenario()


@pytest.mark.parametrize("raw", [b"{broken\n", b"\xff\n", b'{"text":"missing fields"}\n'])
async def test_existing_invalid_outbox_fails_without_send_or_rewrite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    raw: bytes,
) -> None:
    monkeypatch.setenv("AVA_HOME", str(tmp_path))
    path = tmp_path / "state" / "im_bridge" / "outbox.jsonl"
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    gateway = FakeGateway()
    core = _core(gateway)
    with pytest.raises((ValueError, UnicodeDecodeError)):
        await core._replay_outbox_once()
    assert path.read_bytes() == raw
    assert gateway.sent == []
    assert gateway.sent_keys == []
    assert not path.with_suffix(".jsonl.tmp").exists()


def test_absent_outbox_is_optional(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.setenv("AVA_HOME", str(tmp_path))
    assert state_mod._load_outbox() == []
    assert not (tmp_path / "state" / "im_bridge" / "outbox.jsonl").exists()
