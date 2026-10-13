"""Im bridge core cases: the push snapshot watermark is per chat."""

from __future__ import annotations

from services.entrypoints.im_bridge.cursor_store import PushWatermark
from services.entrypoints.im_bridge.tests.task_scope import owned_tasks
from services.entrypoints.im_bridge.tests.test_im_bridge_core import (
    FakeGateway,
    FakePlainAdapter,
    FakeTypingAdapter,
    create_test_core,
)
from services.entrypoints.im_bridge.types import ChatState


async def test_push_snapshot_watermark_is_per_chat() -> None:
    """Regression (audit round 2, P1): the watermark used to be keyed by
    agent alone, so two chats switched to the same agent clobbered each
    other — the second chat's snapshot advanced the shared watermark past
    what the first chat had pushed, and the first chat permanently missed
    those items. The key must be (channel, chat_id, agent_id)."""
    async with owned_tasks() as _owned_tasks:
        gateway = FakeGateway()
        core = create_test_core(gateway, tasks=_owned_tasks)
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
