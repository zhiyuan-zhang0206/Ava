"""Feishu inbound polling resumes from a persisted cursor after a restart.

The platform keeps no offset for the polling path, so a cursor held only in
memory meant every message sent while the bridge was down was never read: the
first round after a restart re-seeded at the newest message. The persisted
cursor lets the next round replay the gap, bounded by the agent side's own
staleness window, and the gateway dedups each message on its idempotency key.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from psycopg_pool import ConnectionPool

from base.config import settings
from services.entrypoints.im_bridge.adapters.feishu import FeishuAdapter
from services.entrypoints.im_bridge.cursor_store import CursorStore
from services.entrypoints.im_bridge.tests.slices import feishu_config
from services.entrypoints.im_bridge.types import InboundMessage
from tests.components.base.poll_until import poll_until_async

_CHAT = "oc_p2p_1"
_H = 3600 * 1000


@pytest.fixture
def store() -> Iterator[CursorStore]:
    """The daemon's shape: one pool on the cluster DB, shared across "restarts"."""
    pool: ConnectionPool[Any] = ConnectionPool(
        settings.data_plane.db_url, min_size=1, max_size=2, open=True
    )
    try:
        yield CursorStore(pool)
    finally:
        pool.close()


class _Core:
    def __init__(self, store: CursorStore) -> None:
        self.cursor_store = store
        self.received: list[InboundMessage] = []

    async def handle_inbound(self, message: InboundMessage) -> None:
        self.received.append(message)


class _Rest:
    def __init__(self) -> None:
        self.responses: list[SimpleNamespace] = []
        self.page_tokens: list[str | None] = []

    @property
    def im(self) -> SimpleNamespace:
        return SimpleNamespace(v1=SimpleNamespace(message=SimpleNamespace(list=self._list)))

    def _list(self, request: Any) -> SimpleNamespace:
        self.page_tokens.append(request.page_token)
        return self.responses.pop(0)


def _item(message_id: str, age_ms: int, text: str = "hi") -> SimpleNamespace:
    return SimpleNamespace(
        message_id=message_id,
        msg_type="text",
        body=SimpleNamespace(content=f'{{"text": "{text}"}}'),
        sender=SimpleNamespace(sender_type="user", id="ou_user_1", id_type="open_id"),
        create_time=str(int(time.time() * 1000) - age_ms),
    )


def _listing(*newest_first: SimpleNamespace, next_page: str | None = None) -> SimpleNamespace:
    data = SimpleNamespace(
        items=list(newest_first), has_more=next_page is not None, page_token=next_page
    )
    return SimpleNamespace(code=0, msg="ok", data=data)


def _adapter(rest: _Rest, store: CursorStore) -> FeishuAdapter:
    adapter = FeishuAdapter(_Core(store), feishu_config())
    adapter._rest_client = rest
    return adapter


async def _restarted(store: CursorStore, *listings: SimpleNamespace) -> FeishuAdapter:
    """A new adapter (daemon restart) whose first round sees `listings` as pages."""
    rest = _Rest()
    rest.responses = list(listings)
    adapter = _adapter(rest, store)
    await adapter._restore_cursors()
    return adapter


async def _first_run(store: CursorStore, *newest_first: SimpleNamespace) -> None:
    rest = _Rest()
    rest.responses = [_listing(*newest_first)]
    adapter = _adapter(rest, store)
    adapter._poll_chats.add(_CHAT)
    await adapter._poll_once(_CHAT)  # seed round persists the cursor


def _texts(adapter: FeishuAdapter) -> list[str]:
    return [m.message_id or "" for m in adapter.core.received]


def _window_ms() -> int:
    return int(feishu_config().delivery_watchdog_stale_claimed_threshold_seconds * 1000)


async def test_restart_replays_messages_that_arrived_during_downtime(store: CursorStore) -> None:
    await _first_run(store, _item("om_1", 5 * _H))
    adapter = await _restarted(
        store, _listing(_item("om_3", 1 * _H), _item("om_2", 2 * _H), _item("om_1", 5 * _H))
    )
    assert _CHAT in adapter._poll_chats  # polled again without an outbound send
    assert await adapter._poll_once(_CHAT)
    assert _texts(adapter) == ["om_2", "om_3"]
    assert [m.idempotency_key for m in adapter.core.received] == ["feishu:om_2", "feishu:om_3"]
    assert adapter._poll_cursor == {_CHAT: "om_3"}


async def test_cursor_survives_a_second_restart(store: CursorStore) -> None:
    await _first_run(store, _item("om_1", 5 * _H))
    first = await _restarted(store, _listing(_item("om_2", 1 * _H), _item("om_1", 5 * _H)))
    await first._poll_once(_CHAT)
    second = await _restarted(store, _listing(_item("om_2", 1 * _H), _item("om_1", 5 * _H)))
    await second._poll_once(_CHAT)
    assert second.core.received == []  # om_2 was handled before the first restart ended


async def test_replay_pages_back_through_a_gap_larger_than_one_page(store: CursorStore) -> None:
    await _first_run(store, _item("om_1", 5 * _H))
    adapter = await _restarted(
        store,
        _listing(_item("om_4", 1 * _H), _item("om_3", 2 * _H), next_page="t1"),
        _listing(_item("om_2", 3 * _H), _item("om_1", 5 * _H), next_page="t2"),
    )
    await adapter._poll_once(_CHAT)
    assert _texts(adapter) == ["om_2", "om_3", "om_4"]
    assert adapter._poll_cursor == {_CHAT: "om_4"}
    assert adapter._rest_client.page_tokens == [None, "t1"]  # stopped at the cursor's page


async def test_replay_stops_paging_at_the_replay_window(store: CursorStore) -> None:
    await _first_run(store, _item("om_1", _window_ms() + 10 * _H))
    adapter = await _restarted(
        store,
        _listing(_item("om_3", 1 * _H), _item("om_2", _window_ms() + 5 * _H), next_page="t1"),
    )
    await adapter._poll_once(_CHAT)
    assert _texts(adapter) == ["om_3"]  # om_2 is past the window and ends the paging
    assert adapter._rest_client.page_tokens == [None]


async def test_a_failed_page_fails_the_round_and_keeps_the_replay(store: CursorStore) -> None:
    await _first_run(store, _item("om_1", 5 * _H))
    failing = SimpleNamespace(code=99, msg="denied", data=None)
    adapter = await _restarted(store, _listing(_item("om_3", 1 * _H), next_page="t1"), failing)
    assert not await adapter._poll_once(_CHAT)
    assert adapter.core.received == []
    assert _CHAT in adapter._poll_replay
    adapter._rest_client.responses = [_listing(_item("om_3", 1 * _H), _item("om_1", 5 * _H))]
    assert await adapter._poll_once(_CHAT)
    assert _texts(adapter) == ["om_3"]


async def test_all_stale_gap_advances_cursor_so_it_is_never_replayed_later(
    store: CursorStore,
) -> None:
    await _first_run(store, _item("om_1", _window_ms() + 10 * _H))
    stale = [_item("om_2", _window_ms() + 5 * _H), _item("om_1", _window_ms() + 10 * _H)]
    adapter = await _restarted(store, _listing(*stale), _listing(*stale))
    await adapter._poll_once(_CHAT)
    assert adapter._poll_cursor == {_CHAT: "om_2"}
    await adapter._poll_once(_CHAT)
    assert adapter.core.received == []


async def test_cursor_rotated_out_of_the_page_replays_only_newer_messages(
    store: CursorStore,
) -> None:
    await _first_run(store, _item("om_old", 3 * _H))
    adapter = await _restarted(
        store,
        _listing(_item("om_b", 1 * _H), _item("om_a", 2 * _H), _item("om_before", 4 * _H)),
    )
    await adapter._poll_once(_CHAT)
    # om_old is not listed; om_before predates the cursor's time and was handled
    assert _texts(adapter) == ["om_a", "om_b"]


async def test_chat_without_a_saved_cursor_still_seeds_without_replaying_history(
    store: CursorStore,
) -> None:
    adapter = await _restarted(store, _listing(_item("om_2", 1 * _H), _item("om_1", 2 * _H)))
    adapter._poll_chats.add(_CHAT)
    await adapter._poll_once(_CHAT)
    assert adapter.core.received == []
    assert adapter._poll_cursor == {_CHAT: "om_2"}


async def test_empty_seed_round_saves_a_position_too(store: CursorStore) -> None:
    """A first round over an empty chat leaves a time-only cursor: the message
    that lands before the next restart is replayed, not mistaken for history."""
    await _first_run(store)
    adapter = await _restarted(store, _listing(_item("om_1", 0)))
    await adapter._poll_once(_CHAT)
    assert _texts(adapter) == ["om_1"]


async def test_the_poll_loop_polls_the_saved_chats_without_an_outbound_send(
    store: CursorStore,
) -> None:
    await _first_run(store, _item("om_1", 5 * _H))
    adapter = _adapter(_Rest(), store)
    adapter._start_poller()
    try:
        await poll_until_async(lambda: adapter._poll_chats == {_CHAT}, what="saved chat polled")
        assert adapter._poll_replay == {_CHAT}
    finally:
        assert adapter._poll_task is not None
        adapter._poll_task.cancel()
