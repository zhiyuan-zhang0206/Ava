"""Durable acceptance retains stamped ordering without a cursor-only rollback."""

import pytest

from services.entrypoints.im_bridge.cursor_store import CursorStore, PushWatermark
from services.entrypoints.im_bridge.outbound_store import TimelineOutboxStore
from services.entrypoints.im_bridge.tests.test_timeline_outbox import candidate
from services.entrypoints.im_bridge.tests.test_timeline_outbox import pool as pool


@pytest.mark.parametrize(
    "saved_id,saved_stamp,item_id,item_stamp,accepted",
    [
        ("9.5", None, "10.1", None, True),
        ("10.1", None, "9.9", None, False),
        ("377.1", "2026-10-03T10:00:00Z", "1.0", "2026-10-03T11:00:00Z", True),
        ("377.1", "2026-10-03T11:00:00Z", "1.0", "2026-10-03T10:00:00Z", False),
        ("1.0", "2026-10-03T11:00:00Z", "1.0", "2026-10-03T11:00:00Z", False),
    ],
)
def test_durable_cursor_orders_compact_and_numeric_tail(
    pool, saved_id, saved_stamp, item_id, item_stamp, accepted
) -> None:
    cursors = CursorStore(pool)
    cursors.save_push("telegram", "chat", 7, PushWatermark(saved_stamp, saved_id))
    item = candidate()
    item.item["item_id"] = item_id
    if item_stamp is not None:
        item.item["created_at"] = item_stamp
    result = TimelineOutboxStore(pool).accept("telegram", "bot", "chat", 7, [item])
    assert bool(result.intent_ids) is accepted
    expected = (
        PushWatermark(item_stamp, item_id) if accepted else PushWatermark(saved_stamp, saved_id)
    )
    assert cursors.load_push() == {("telegram", "chat", 7): expected}


def test_legacy_numbering_rollback_cannot_advance_without_an_intent(pool) -> None:
    cursors = CursorStore(pool)
    original = PushWatermark(None, "377.1")
    cursors.save_push("telegram", "chat", 7, original)
    item = candidate(128, qualified=False)
    result = TimelineOutboxStore(pool).accept("telegram", "bot", "chat", 7, [item])
    assert not result.intent_ids
    assert cursors.load_push() == {("telegram", "chat", 7): original}


async def test_equal_stamp_blocks_keep_numeric_order_and_distinct_source_ordinals() -> None:
    from services.entrypoints.im_bridge.tests.test_im_bridge_core import FakeGateway, _core

    core = _core(FakeGateway())
    state = core._get_or_create_state("telegram", "chat")
    state.current_agent_id = 7
    stamp = "2026-10-03T11:00:00Z"
    core.cursor_store.save_push("telegram", "chat", 7, PushWatermark(stamp, "1.8"))
    core.gateway.timeline = [
        {
            "kind": "agent_chat",
            "item_id": f"1.{block}",
            "created_at": stamp,
            "payload": f"block {block}",
            "source_message_id": "one-persisted-message",
            "source_block_idx": block,
        }
        for block in (10, 9)
    ]
    await core._push_snapshot(("telegram", "chat"), state, {})
    assert core.cursor_store.load_push() == {("telegram", "chat", 7): PushWatermark(stamp, "1.10")}
    await core.timeline_worker.run_once()
    await core.timeline_worker.run_once()
    assert core.adapters["telegram"].sent == [
        ("chat", "[Ava #7] block 9"),
        ("chat", "[Ava #7] block 10"),
    ]
    await core._push_snapshot(("telegram", "chat"), state, {})
    assert core.timeline_outbox.pending_streams({"telegram": "test-account"}) == []
