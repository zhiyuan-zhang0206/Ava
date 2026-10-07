"""Feishu adapter cases: poll feeds messages newer than cursor in."""

from __future__ import annotations

import asyncio
from collections import deque
from types import SimpleNamespace

from services.entrypoints.im_bridge.adapters.feishu import FeishuAdapter, _backoff_delay
from services.entrypoints.im_bridge.adapters.tests.test_feishu_adapter import (
    BlockingThread,
    FakeCore,
    FakeRestClient,
    make_event,
    make_list_item,
    make_list_item_listapi,
    make_list_response,
)
from services.entrypoints.im_bridge.adapters.tests.test_feishu_adapter import (
    adapter as adapter,
)
from services.entrypoints.im_bridge.tests.slices import feishu_config
from services.entrypoints.im_bridge.types import InboundMessage


async def test_poll_feeds_messages_newer_than_cursor_in_order(adapter: FeishuAdapter) -> None:
    rest = FakeRestClient()
    adapter._rest_client = rest
    adapter._poll_chats.add("oc_p2p_1")
    # Round 1: seed cursor at om_2.
    rest.list_responses = [
        make_list_response(
            [
                make_list_item(message_id="om_2", content='{"text": "old"}'),
                make_list_item(message_id="om_1"),
            ]
        )
    ]
    await adapter._poll_once("oc_p2p_1")
    assert adapter.core.received == []
    # Round 2: om_3 and om_4 arrive; only they are fed, in order.
    rest.list_responses = [
        make_list_response(
            [
                make_list_item(message_id="om_4", content='{"text": "four"}'),
                make_list_item(message_id="om_3", content='{"text": "three"}'),
                make_list_item(message_id="om_2", content='{"text": "old"}'),
                make_list_item(message_id="om_1"),
            ]
        )
    ]
    await adapter._poll_once("oc_p2p_1")
    assert [m.text for m in adapter.core.received] == ["three", "four"]
    assert [m.chat_id for m in adapter.core.received] == ["ou_user_1", "ou_user_1"]
    assert adapter._poll_cursor == {"oc_p2p_1": "om_4"}


async def test_poll_skips_own_sends_and_non_text(adapter: FeishuAdapter) -> None:
    rest = FakeRestClient()
    adapter._rest_client = rest
    adapter._poll_chats.add("oc_p2p_1")
    rest.list_responses = [
        # Round 1: seed the cursor at the old boundary.
        make_list_response([make_list_item(message_id="om_0")]),
        # Round 2: bot send + image + user text arrive; only the user text feeds.
        make_list_response(
            [
                make_list_item(
                    message_id="om_9", content='{"text": "from bot"}', sender_type="app"
                ),
                make_list_item(message_id="om_8", content='{"image_key": "x"}', msg_type="image"),
                make_list_item(message_id="om_7", content='{"text": "user text"}'),
                make_list_item(message_id="om_0"),
            ]
        ),
    ]
    await adapter._poll_once("oc_p2p_1")
    await adapter._poll_once("oc_p2p_1")
    assert [m.text for m in adapter.core.received] == ["user text"]


async def test_poll_dedups_by_message_id(adapter: FeishuAdapter) -> None:
    """The same message seen twice (list window overlap) feeds core once."""
    rest = FakeRestClient()
    adapter._rest_client = rest
    adapter._poll_chats.add("oc_p2p_1")
    item = make_list_item(message_id="om_x", content='{"text": "dup"}')
    rest.list_responses = [
        # Round 1: seed the cursor at the old boundary.
        make_list_response([make_list_item(message_id="om_0")]),
        # Round 2: om_x arrives — fed once, cursor advances.
        make_list_response([item, make_list_item(message_id="om_0")]),
        # Round 3: same window again — om_x already seen, not re-fed.
        make_list_response([item, make_list_item(message_id="om_0")]),
    ]
    await adapter._poll_once("oc_p2p_1")
    await adapter._poll_once("oc_p2p_1")
    await adapter._poll_once("oc_p2p_1")
    assert len(adapter.core.received) == 1
    assert [m.text for m in adapter.core.received] == ["dup"]


async def test_poll_and_ws_paths_are_idempotent(adapter: FeishuAdapter) -> None:
    """The WS event path must not double-feed a message the poller delivered."""
    rest = FakeRestClient()
    adapter._rest_client = rest
    adapter._poll_chats.add("oc_p2p_1")
    rest.list_responses = [
        # Round 1: seed at the old boundary.
        make_list_response([make_list_item(message_id="om_0")]),
        # Round 2: the new message arrives via the poller.
        make_list_response(
            [
                make_list_item(message_id="om_same", content='{"text": "once"}'),
                make_list_item(message_id="om_0"),
            ]
        ),
    ]
    await adapter._poll_once("oc_p2p_1")
    await adapter._poll_once("oc_p2p_1")
    assert len(adapter.core.received) == 1
    # The same message arrives via the WS path afterwards — must be dropped.
    await adapter._handle_event(make_event(message_id="om_same", content='{"text": "once"}'))
    assert len(adapter.core.received) == 1


async def test_send_registers_chat_for_polling(adapter: FeishuAdapter) -> None:
    """An outbound send resolves the p2p chat id and adds it to the poll set."""
    adapter._app_id = "cli_x"
    adapter._app_secret = "secret_x"  # noqa: S105
    thread = BlockingThread()
    thread.start()
    adapter._ws_thread = thread
    rest = FakeRestClient()
    adapter._rest_client = rest
    try:
        await adapter.send("ou_user_1", "hi")
    finally:
        thread.release()
        thread.join(timeout=2)
    assert adapter._sent_chat_ids == {"ou_user_1": "oc_p2p_1"}
    assert "oc_p2p_1" in adapter._poll_chats


async def test_send_restores_owner_open_id_when_unseeded(adapter: FeishuAdapter) -> None:
    """An outbound send restores the owner open id when the seed found no user."""
    adapter._app_id = "cli_x"
    adapter._app_secret = "secret_x"  # noqa: S105
    thread = BlockingThread()
    thread.start()
    adapter._ws_thread = thread
    rest = FakeRestClient()
    adapter._rest_client = rest
    try:
        assert adapter._last_open_id == ""
        await adapter.send("ou_user_1", "hi")
    finally:
        thread.release()
        thread.join(timeout=2)
    assert adapter._last_open_id == "ou_user_1"


async def test_send_does_not_override_existing_owner_open_id(adapter: FeishuAdapter) -> None:
    """An outbound send preserves an existing owner open id."""
    adapter._app_id = "cli_x"
    adapter._app_secret = "secret_x"  # noqa: S105
    thread = BlockingThread()
    thread.start()
    adapter._ws_thread = thread
    rest = FakeRestClient()
    adapter._rest_client = rest
    adapter._last_open_id = "ou_first"
    try:
        await adapter.send("ou_other", "hi")
    finally:
        thread.release()
        thread.join(timeout=2)
    assert adapter._last_open_id == "ou_first"


def test_start_poller_honors_zero_interval() -> None:
    """AVA_FEISHU_POLL_INTERVAL_SECONDS=0 disables the poller (WS-only)."""
    adapter = FeishuAdapter(FakeCore(), feishu_config(feishu_poll_interval_seconds=0))
    adapter._start_poller()
    assert adapter._poll_task is None


async def test_poll_seed_restores_owner_open_id_after_restart(adapter: FeishuAdapter) -> None:
    """After a daemon restart the owner open id is gone from memory; the
    seed round restores it from the newest user message in the window so
    outbound notifications do not fail until the user's next message."""
    rest = FakeRestClient()
    adapter._rest_client = rest
    adapter._poll_chats.add("oc_p2p_1")
    rest.list_responses = [
        make_list_response(
            [
                make_list_item(message_id="om_3", open_id="ou_owner"),
                make_list_item(message_id="om_2"),
            ]
        )
    ]
    assert adapter._last_open_id == ""
    await adapter._poll_once("oc_p2p_1")
    assert adapter._last_open_id == "ou_owner"
    assert adapter.core.received == []  # seed still delivers nothing


async def test_poll_seed_restores_open_id_from_list_api_shape(adapter: FeishuAdapter) -> None:
    """The restore path also understands the ListMessage sender shape."""
    rest = FakeRestClient()
    adapter._rest_client = rest
    adapter._poll_chats.add("oc_p2p_1")
    rest.list_responses = [
        make_list_response(
            [
                make_list_item_listapi(message_id="om_5", sender_id="ou_listowner"),
                make_list_item_listapi(message_id="om_4"),
            ]
        )
    ]
    await adapter._poll_once("oc_p2p_1")
    assert adapter._last_open_id == "ou_listowner"


async def test_poll_seed_ignores_app_messages_for_owner_restore(
    adapter: FeishuAdapter,
) -> None:
    """Bot sends in the window must not masquerade as the owner."""
    rest = FakeRestClient()
    adapter._rest_client = rest
    adapter._poll_chats.add("oc_p2p_1")
    rest.list_responses = [
        make_list_response(
            [
                make_list_item_listapi(message_id="om_6", sender_id="ou_app", sender_type="app"),
                make_list_item_listapi(message_id="om_5", sender_id="ou_owner"),
            ]
        )
    ]
    await adapter._poll_once("oc_p2p_1")
    assert adapter._last_open_id == "ou_owner"


async def test_poll_normalizes_list_message_api_shape(adapter: FeishuAdapter) -> None:
    """Regression: ListMessage items carry no chat_type and put the open id
    on sender.id (id_type=open_id) — the poller must deliver them, not
    reject them as non-p2p / id-less (post-deploy bug: every polled message
    was dropped since 2026-09-02 04:08)."""
    rest = FakeRestClient()
    adapter._rest_client = rest
    adapter._poll_chats.add("oc_p2p_1")
    rest.list_responses = [
        make_list_response([make_list_item_listapi(message_id="om_0")]),
        make_list_response(
            [
                make_list_item_listapi(message_id="om_9", content='{"text": "via list api"}'),
                make_list_item_listapi(message_id="om_0"),
            ]
        ),
    ]
    await adapter._poll_once("oc_p2p_1")
    await adapter._poll_once("oc_p2p_1")
    assert [m.text for m in adapter.core.received] == ["via list api"]
    assert [m.chat_id for m in adapter.core.received] == ["ou_user_1"]
    assert adapter._last_open_id == "ou_user_1"


async def test_poll_list_api_item_with_union_id_rejected(adapter: FeishuAdapter) -> None:
    """A ListMessage sender whose id is not an open_id is not deliverable."""
    rest = FakeRestClient()
    adapter._rest_client = rest
    adapter._poll_chats.add("oc_p2p_1")
    rest.list_responses = [
        make_list_response([make_list_item_listapi(message_id="om_0")]),
        make_list_response(
            [
                make_list_item_listapi(
                    message_id="om_8",
                    content='{"text": "union id"}',
                    sender_id="on_union_1",
                    id_type="union_id",
                ),
                make_list_item_listapi(message_id="om_0"),
            ]
        ),
    ]
    await adapter._poll_once("oc_p2p_1")
    await adapter._poll_once("oc_p2p_1")
    assert adapter.core.received == []
    # The union-id message must not replace the seed-restored owner id.
    assert adapter._last_open_id == "ou_user_1"


def test_poll_round_delay_partial_failure_keeps_cadence(adapter: FeishuAdapter) -> None:
    """D1: one chat's persistent failure must not slow the healthy chats —
    only an all-chats-failed round backs off (and resets the counter)."""
    adapter._poll_failures = 2  # prior all-chats-failed rounds
    assert adapter._round_delay(failed=1, total=2, interval=1.0) == 1.0
    assert adapter._poll_failures == 0


def test_poll_round_delay_healthy_round_resets(adapter: FeishuAdapter) -> None:
    adapter._poll_failures = 3
    assert adapter._round_delay(failed=0, total=2, interval=1.0) == 1.0
    assert adapter._poll_failures == 0


def test_poll_round_delay_all_chats_failed_backs_off(adapter: FeishuAdapter) -> None:
    """D1: when every chat fails the round, the delay backs off exponentially
    (interval x2, x4, x8) and the counter accumulates."""
    assert adapter._round_delay(failed=2, total=2, interval=1.0) == 2.0
    assert adapter._round_delay(failed=2, total=2, interval=1.0) == 4.0
    assert adapter._round_delay(failed=2, total=2, interval=1.0) == 8.0
    assert adapter._poll_failures == 3


def test_backoff_delay_never_clamps_above_interval() -> None:
    """D2: the absolute cap never drops below the configured interval — a
    3600s interval keeps its own cadence instead of being clamped to 300s."""
    assert _backoff_delay(3600.0, 0) == 3600.0
    assert _backoff_delay(3600.0, 5) == 3600.0
    assert _backoff_delay(1.0, 0) == 1.0
    assert _backoff_delay(1.0, 5) == 32.0
    assert _backoff_delay(300.0, 6) == 300.0


async def test_poll_seed_anchors_newest_id_carrying_item(adapter: FeishuAdapter) -> None:
    """D3: an id-less newest item (defensive) must not leave the seed round
    without a cursor — anchor on the newest item that carries an id."""
    rest = FakeRestClient()
    adapter._rest_client = rest
    adapter._poll_chats.add("oc_p2p_1")
    rest.list_responses = [
        make_list_response(
            [
                make_list_item(message_id=None, content='{"text": "no id"}'),
                make_list_item(message_id="om_3"),
                make_list_item(message_id="om_2"),
                make_list_item(message_id="om_1"),
            ]
        )
    ]
    await adapter._poll_once("oc_p2p_1")
    assert adapter.core.received == []
    assert adapter._poll_cursor == {"oc_p2p_1": "om_3"}


async def test_poll_poison_message_skipped_after_retries(adapter: FeishuAdapter) -> None:
    """D4: a message that keeps crashing inbound handling is skipped after
    POISON_MAX_RETRIES consecutive failures instead of wedging the chat."""

    class AlwaysFailingCore(FakeCore):
        async def handle_inbound(self, message: InboundMessage) -> None:
            raise RuntimeError("boom")

    core = AlwaysFailingCore()
    rest = FakeRestClient()
    adapter = FeishuAdapter(core, feishu_config())
    adapter._rest_client = rest
    adapter._poll_chats.add("oc_p2p_1")
    window = make_list_response(
        [
            make_list_item(message_id="om_5", content='{"text": "poison"}'),
            make_list_item(message_id="om_0"),
        ]
    )
    rest.list_responses = [
        make_list_response([make_list_item(message_id="om_0")]),
        window,
        window,
        window,
    ]
    await adapter._poll_once("oc_p2p_1")  # seed at om_0
    await adapter._poll_once("oc_p2p_1")  # om_5 fails (1)
    assert adapter._poll_cursor == {"oc_p2p_1": "om_0"}
    await adapter._poll_once("oc_p2p_1")  # om_5 fails (2)
    assert adapter._poll_cursor == {"oc_p2p_1": "om_0"}
    assert adapter._poison_retries == {"oc_p2p_1:om_5": 2}
    await adapter._poll_once("oc_p2p_1")  # om_5 skipped after 3rd failure
    assert adapter._poll_cursor == {"oc_p2p_1": "om_5"}
    assert adapter._poison_retries == {}
    assert core.received == []


async def test_poll_empty_seed_round_delivers_first_message(adapter: FeishuAdapter) -> None:
    """P1 regression: an empty seeding round must not swallow the first real
    message. A fresh chat's first list round is empty; the next round's first
    message would hit the seed branch and only plant a cursor — now the chat
    is marked seeded by the empty round, so the message is delivered."""
    rest = FakeRestClient()
    adapter._rest_client = rest
    adapter._poll_chats.add("oc_p2p_1")
    rest.list_responses = [
        make_list_response([]),
        make_list_response([make_list_item(message_id="om_1", content='{"text": "first"}')]),
    ]
    await adapter._poll_once("oc_p2p_1")
    assert adapter.core.received == []
    await adapter._poll_once("oc_p2p_1")
    assert [m.text for m in adapter.core.received] == ["first"]
    assert adapter._poll_cursor == {"oc_p2p_1": "om_1"}


async def test_poll_failed_list_round_does_not_seed(adapter: FeishuAdapter) -> None:
    """A failed list round returns False and neither seeds the chat nor moves
    the cursor — the no-replay guarantee survives API hiccups (backoff
    trigger for the poll loop)."""
    rest = FakeRestClient()
    adapter._rest_client = rest
    adapter._poll_chats.add("oc_p2p_1")
    rest.list_responses = [
        SimpleNamespace(code=500, msg="boom", data=None),
        make_list_response(
            [
                make_list_item(message_id="om_3"),
                make_list_item(message_id="om_2"),
                make_list_item(message_id="om_1"),
            ]
        ),
    ]
    assert await adapter._poll_once("oc_p2p_1") is False
    assert adapter._poll_seeded == set()
    assert adapter._poll_cursor == {}
    # Next round still seeds (no replay of history).
    assert await adapter._poll_once("oc_p2p_1") is True
    assert adapter.core.received == []
    assert adapter._poll_cursor == {"oc_p2p_1": "om_3"}


async def test_poll_idless_items_never_delivered_or_cursored(adapter: FeishuAdapter) -> None:
    """P2: items without a message id are rejected (no dedup possible) and
    must not poison the cursor or be fed to core."""
    rest = FakeRestClient()
    adapter._rest_client = rest
    adapter._poll_chats.add("oc_p2p_1")
    rest.list_responses = [
        make_list_response([make_list_item(message_id="om_0")]),
        make_list_response(
            [
                make_list_item(message_id="om_2", content='{"text": "two"}'),
                make_list_item(message_id=None, content='{"text": "no-id"}'),
                make_list_item(message_id="om_0"),
            ]
        ),
    ]
    await adapter._poll_once("oc_p2p_1")
    await adapter._poll_once("oc_p2p_1")
    assert [m.text for m in adapter.core.received] == ["two"]
    assert adapter._poll_cursor == {"oc_p2p_1": "om_2"}


async def test_poll_inbound_failure_retried_next_round(adapter: FeishuAdapter) -> None:
    """P2: the cursor only advances past delivered messages — a failed
    handle_inbound is retried on the next round instead of being skipped."""

    class FlakyCore(FakeCore):
        def __init__(self) -> None:
            super().__init__()
            self.fail_first = True

        async def handle_inbound(self, message: InboundMessage) -> None:
            if self.fail_first:
                self.fail_first = False
                raise RuntimeError("boom")
            self.received.append(message)

    core = FlakyCore()
    rest = FakeRestClient()
    adapter = FeishuAdapter(core, feishu_config())
    adapter._rest_client = rest
    adapter._poll_chats.add("oc_p2p_1")
    failing_window = make_list_response(
        [
            make_list_item(message_id="om_5", content='{"text": "flaky"}'),
            make_list_item(message_id="om_0"),
        ]
    )
    rest.list_responses = [
        make_list_response([make_list_item(message_id="om_0")]),
        failing_window,
        failing_window,
    ]
    await adapter._poll_once("oc_p2p_1")  # seed at om_0
    await adapter._poll_once("oc_p2p_1")  # om_5 fails to deliver
    assert adapter._poll_cursor == {"oc_p2p_1": "om_0"}
    assert core.received == []
    await adapter._poll_once("oc_p2p_1")  # retried and delivered
    assert adapter._poll_cursor == {"oc_p2p_1": "om_5"}
    assert [m.text for m in core.received] == ["flaky"]


async def test_poll_never_marks_seen_before_delivery(adapter: FeishuAdapter) -> None:
    """The seen-set is only written after handle_inbound succeeds, so a
    WS-path redelivery of a failed poll item is not deduped away."""

    class FailingCore(FakeCore):
        async def handle_inbound(self, message: InboundMessage) -> None:
            raise RuntimeError("boom")

    core = FailingCore()
    rest = FakeRestClient()
    adapter = FeishuAdapter(core, feishu_config())
    adapter._rest_client = rest
    adapter._poll_chats.add("oc_p2p_1")
    rest.list_responses = [
        make_list_response([make_list_item(message_id="om_0")]),
        make_list_response(
            [
                make_list_item(message_id="om_9", content='{"text": "will fail"}'),
                make_list_item(message_id="om_0"),
            ]
        ),
    ]
    await adapter._poll_once("oc_p2p_1")
    await adapter._poll_once("oc_p2p_1")
    assert adapter._seen_messages == deque()
    assert adapter._poll_cursor == {"oc_p2p_1": "om_0"}


async def test_card_send_registers_chat_for_polling(adapter: FeishuAdapter) -> None:
    """P2: a card (interactive) send resolves the p2p chat id exactly like a
    text send, so button-reply conversations also enable polling."""
    adapter._app_id = "cli_x"
    adapter._app_secret = "secret_x"  # noqa: S105
    thread = BlockingThread()
    thread.start()
    adapter._ws_thread = thread
    rest = FakeRestClient()
    adapter._rest_client = rest
    try:
        await adapter.send("ou_user_1", "pick:", buttons=[("a", "/status")])
    finally:
        thread.release()
        thread.join(timeout=2)
    assert adapter._sent_chat_ids == {"ou_user_1": "oc_p2p_1"}
    assert "oc_p2p_1" in adapter._poll_chats


async def test_stop_cancels_poller_task(adapter: FeishuAdapter) -> None:
    """P2: stop() cancels the polling task so a daemon shutdown does not leak
    an endless loop."""
    rest = FakeRestClient()
    adapter._rest_client = rest
    adapter._poll_chats.add("oc_p2p_1")
    adapter._poll_task = asyncio.create_task(adapter._poll_loop(0.01))
    await adapter.stop()
    assert adapter._poll_task is None
    await asyncio.sleep(0.05)
    assert adapter._poll_task is None
