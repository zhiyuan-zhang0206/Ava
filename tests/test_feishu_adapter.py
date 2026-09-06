"""Tests for the IM Bridge Feishu adapter.

The lark-oapi SDK is mocked at the adapter seam: inbound events are duck-typed
stand-ins shaped like ``P2ImMessageReceiveV1`` (the SDK's protobuf-style
objects are awkward to construct by hand), and the ws/REST clients are fakes —
no network and no real WebSocket in these tests.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections import deque
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from services.im_bridge.adapters.feishu import (
    MAX_SEGMENT_CHARS,
    FeishuAdapter,
    _backoff_delay,
    _segment,
)
from services.im_bridge.types import InboundMessage


async def _wait_until(
    predicate: Callable[[], bool], timeout: float = 30.0, interval: float = 0.01
) -> None:
    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(interval)

    await asyncio.wait_for(_poll(), timeout=timeout)


class FakeCore:
    def __init__(self) -> None:
        self.received: list[InboundMessage] = []

    async def handle_inbound(self, message: InboundMessage) -> None:
        self.received.append(message)


def make_event(
    *,
    chat_type: str = "p2p",
    message_type: str = "text",
    content: str = '{"text": "hello feishu"}',
    open_id: str = "ou_user_1",
    message_id: str = "om_msg_1",
    sender_type: str = "user",
) -> SimpleNamespace:
    """A duck-typed stand-in for lark's P2ImMessageReceiveV1."""
    return SimpleNamespace(
        event=SimpleNamespace(
            message=SimpleNamespace(
                message_id=message_id,
                chat_type=chat_type,
                message_type=message_type,
                content=content,
            ),
            sender=SimpleNamespace(
                sender_id=SimpleNamespace(open_id=open_id),
                sender_type=sender_type,
            ),
        ),
        header=SimpleNamespace(event_id="evt_1"),
    )


class FakeWsClient:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.disconnected = threading.Event()

    def start(self) -> None:
        self.started.set()

    async def _disconnect(self) -> None:
        self.disconnected.set()


class FakeRestClient:
    """Mimics lark's ``client.im.v1.message`` chain."""

    def __init__(self) -> None:
        self.created: list[Any] = []
        self.fail = False
        self.list_responses: list[SimpleNamespace] = []

    @property
    def im(self) -> SimpleNamespace:
        return SimpleNamespace(
            v1=SimpleNamespace(message=SimpleNamespace(create=self._create, list=self._list))
        )

    def _create(self, request: Any) -> SimpleNamespace:
        self.created.append(request)
        if self.fail:
            return SimpleNamespace(success=lambda: False, code=99999, msg="denied")
        # A send resolves its p2p chat id (real responses carry chat_id).
        return SimpleNamespace(
            success=lambda: True,
            code=0,
            msg="ok",
            data=SimpleNamespace(message_id="om_sent_1", chat_id="oc_p2p_1"),
        )

    def _list(self, request: Any) -> SimpleNamespace:
        if not self.list_responses:
            return SimpleNamespace(code=0, msg="ok", data=None)
        return self.list_responses.pop(0)


class BlockingThread(threading.Thread):
    """A live daemon thread (is_alive() True) the adapter's start-state checks need."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self._release = threading.Event()

    def run(self) -> None:
        self._release.wait()

    def release(self) -> None:
        self._release.set()


class PatchingAdapter(FeishuAdapter):
    """Adapter with the lark ws client construction replaced by a fake."""

    def __init__(self, core: FakeCore, ws_client: FakeWsClient) -> None:
        super().__init__(core)
        self._ws_client_impl = ws_client

    def _build_ws_client(self) -> Any:
        return self._ws_client_impl


@pytest.fixture
def adapter() -> FeishuAdapter:
    return FeishuAdapter(FakeCore())


# -- inbound ---------------------------------------------------------------


async def test_p2p_text_forwarded_to_core(adapter: FeishuAdapter) -> None:
    await adapter._handle_event(make_event())
    assert len(adapter.core.received) == 1
    message = adapter.core.received[0]
    assert message.channel == "feishu"
    assert message.chat_id == "ou_user_1"
    assert message.text == "hello feishu"
    assert message.message_id == "om_msg_1"


@pytest.mark.parametrize("chat_type", ["group", "chat", "p2p_group"])
async def test_group_chat_ignored(adapter: FeishuAdapter, chat_type: str) -> None:
    await adapter._handle_event(make_event(chat_type=chat_type))
    assert adapter.core.received == []


@pytest.mark.parametrize("message_type", ["image", "post", "file", "audio", "media"])
async def test_non_text_messages_ignored(adapter: FeishuAdapter, message_type: str) -> None:
    await adapter._handle_event(
        make_event(message_type=message_type, content='{"image_key": "img_v1"}')
    )
    assert adapter.core.received == []


async def test_malformed_content_ignored(adapter: FeishuAdapter) -> None:
    await adapter._handle_event(make_event(content="not-json"))
    assert adapter.core.received == []


async def test_blank_text_ignored(adapter: FeishuAdapter) -> None:
    await adapter._handle_event(make_event(content='{"text": "   "}'))
    assert adapter.core.received == []


async def test_missing_open_id_ignored(adapter: FeishuAdapter) -> None:
    await adapter._handle_event(make_event(open_id=""))
    assert adapter.core.received == []


async def test_bot_own_message_ignored(adapter: FeishuAdapter) -> None:
    # The bot's own outgoing messages also fire receive_v1; without this guard
    # they would echo back into core forever.
    await adapter._handle_event(make_event(sender_type="app"))
    assert adapter.core.received == []


async def test_ws_callback_dispatches_on_main_loop(
    adapter: FeishuAdapter,
) -> None:
    adapter._main_loop = asyncio.get_running_loop()
    adapter._on_im_message(make_event())
    await _wait_until(lambda: bool(adapter.core.received))
    assert len(adapter.core.received) == 1


async def test_ws_callback_drops_when_no_main_loop(adapter: FeishuAdapter) -> None:
    adapter._on_im_message(make_event())
    # The no-loop path drops synchronously, so no delivery can arrive later.
    await asyncio.sleep(0.05)
    assert adapter.core.received == []


# -- credentials / lifecycle ------------------------------------------------


def test_credentials_read_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared.config import settings

    monkeypatch.setattr(settings.feishu, "feishu_app_id", "cli_env")
    monkeypatch.setattr(settings.feishu, "feishu_app_secret", "sec_ava")
    try:
        adapter = FeishuAdapter(FakeCore())
        assert adapter._credential("feishu_app_id") == "cli_env"
        assert adapter._credential("feishu_app_secret") == "sec_ava"
    finally:
        monkeypatch.undo()


def test_credentials_missing_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared.config import settings

    monkeypatch.setattr(settings.feishu, "feishu_app_id", "")
    monkeypatch.setattr(settings.feishu, "feishu_app_secret", "")
    try:
        adapter = FeishuAdapter(FakeCore())
        assert adapter._credential("feishu_app_id") == ""
        assert adapter._credential("feishu_app_secret") == ""
    finally:
        monkeypatch.undo()


async def test_start_skips_without_credentials(
    adapter: FeishuAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shared.config import settings

    monkeypatch.setattr(settings.feishu, "feishu_app_id", "")
    monkeypatch.setattr(settings.feishu, "feishu_app_secret", "")
    try:
        await adapter.start()
        assert adapter._ws_thread is None
        assert adapter._ws_client is None
    finally:
        monkeypatch.undo()


async def test_start_connects_with_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.config import settings

    monkeypatch.setattr(settings.feishu, "feishu_app_id", "cli_x")
    monkeypatch.setattr(settings.feishu, "feishu_app_secret", "secret_x")
    # The ws thread's first act is a COLD import of lark_oapi.ws.client (a
    # protobuf + websocket import chain, seconds on a loaded CI runner) before
    # the fake's start() can run, so the timed wait below would race that
    # import. Warm it here — inside the running loop, so the SDK's module-level
    # asyncio.get_event_loop() binds without a deprecation path — leaving the
    # wait to cover only thread startup. Production is untouched: the adapter
    # still imports lark lazily in the ws thread (see FeishuAdapter docstring).
    import lark_oapi.ws.client  # noqa: F401  # pyright: ignore[reportUnusedImport]

    ws_client = FakeWsClient()
    adapter = PatchingAdapter(FakeCore(), ws_client)
    await adapter.start()
    assert adapter._ws_thread is not None
    assert ws_client.started.wait(timeout=15)
    assert adapter._ws_client is ws_client
    # Point stop() at the live pytest loop so the scheduled disconnect actually
    # executes (the fake's ws loop never runs); it must return without raising.
    adapter._ws_loop = asyncio.get_running_loop()
    await asyncio.wait_for(adapter.stop(), timeout=30.0)
    await _wait_until(ws_client.disconnected.is_set)
    assert ws_client.disconnected.is_set()


# -- outbound ---------------------------------------------------------------


async def test_send_segments_long_text(adapter: FeishuAdapter) -> None:
    adapter._app_id = "cli_x"
    adapter._app_secret = "secret_x"  # noqa: S105
    thread = BlockingThread()
    thread.start()
    adapter._ws_thread = thread
    rest = FakeRestClient()
    adapter._rest_client = rest
    try:
        text = "a" * (MAX_SEGMENT_CHARS * 2 + 123)
        await adapter.send("ou_user_1", text)
    finally:
        thread.release()
        thread.join(timeout=2)
    assert len(rest.created) == 3
    for request, expected in zip(rest.created, _segment(text, MAX_SEGMENT_CHARS), strict=True):
        assert request.receive_id_type == "open_id"
        assert request.request_body.receive_id == "ou_user_1"
        assert request.request_body.msg_type == "text"
        assert json.loads(request.request_body.content)["text"] == expected


async def test_send_short_text_single_call(adapter: FeishuAdapter) -> None:
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
    assert len(rest.created) == 1


async def test_send_failure_raises_sanitized(adapter: FeishuAdapter) -> None:
    adapter._app_id = "cli_x"
    adapter._app_secret = "secret_x"  # noqa: S105
    thread = BlockingThread()
    thread.start()
    adapter._ws_thread = thread
    rest = FakeRestClient()
    rest.fail = True
    adapter._rest_client = rest
    try:
        with pytest.raises(RuntimeError, match="code=99999"):
            await adapter.send("ou_user_1", "hi")
    finally:
        thread.release()
        thread.join(timeout=2)


async def test_send_without_credentials_raises(adapter: FeishuAdapter) -> None:
    with pytest.raises(RuntimeError, match="not configured"):
        await adapter.send("ou_user_1", "hi")


async def test_send_before_start_raises(adapter: FeishuAdapter) -> None:
    adapter._app_id = "cli_x"
    adapter._app_secret = "secret_x"  # noqa: S105
    with pytest.raises(RuntimeError, match="not started"):
        await adapter.send("ou_user_1", "hi")


async def test_send_to_owner_before_any_inbound_raises_clear_error(
    adapter: FeishuAdapter,
) -> None:
    """send_to_owner with no known p2p chat (daemon restart, no inbound yet)
    raises the documented RuntimeError — not an AttributeError from an
    uninitialized _last_open_id."""
    with pytest.raises(RuntimeError, match="no known user chat"):
        await adapter.send_to_owner("hi")


async def test_send_to_owner_after_inbound_uses_last_open_id(
    adapter: FeishuAdapter,
) -> None:
    """The p2p peer of the last inbound message is the notify target."""
    await adapter._handle_event(make_event(open_id="ou_latest"))
    adapter._app_id = "cli_x"
    adapter._app_secret = "secret_x"  # noqa: S105
    thread = BlockingThread()
    thread.start()
    adapter._ws_thread = thread
    rest = FakeRestClient()
    adapter._rest_client = rest
    try:
        await adapter.send_to_owner("hi")
    finally:
        thread.release()
        thread.join(timeout=2)
    assert len(rest.created) == 1
    assert rest.created[0].request_body.receive_id == "ou_latest"


def test_segment_chunks() -> None:
    assert _segment("", 10) == []
    assert _segment("abc", 10) == ["abc"]
    assert _segment("abcdefghij", 4) == ["abcd", "efgh", "ij"]


# -- REST timeout (task #698 G6) -------------------------------------------


def test_rest_client_applies_configured_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The outbound REST client pins the configured timeout explicitly.

    The lark SDK's own default (30s) is an SDK-version property, not a
    contract — the adapter must pass AVA_FEISHU_REST_TIMEOUT_SECONDS through
    so a hung Feishu line cannot park an IM outbound at an unknown default.
    """
    from shared.config import settings

    monkeypatch.setattr(settings.feishu, "feishu_rest_timeout_seconds", 7.5)
    seen: list[float] = []

    class _Builder:
        def app_id(self, value: str) -> _Builder:
            return self

        def app_secret(self, value: str) -> _Builder:
            return self

        def timeout(self, value: float) -> _Builder:
            seen.append(value)
            return self

        def build(self) -> object:
            return object()

    import lark_oapi

    monkeypatch.setattr(lark_oapi.Client, "builder", staticmethod(_Builder))
    adapter = FeishuAdapter(FakeCore())
    adapter._app_id = "cli_x"
    adapter._app_secret = "secret_x"  # noqa: S105
    client = adapter._build_rest_client()
    assert client is not None
    assert seen == [7.5]


async def test_send_with_buttons_renders_interactive_card(adapter: FeishuAdapter) -> None:
    """Buttons render as an interactive card; the callback value is the
    command string so core routing handles the tap unchanged."""
    adapter._app_id = "cli_x"
    adapter._app_secret = "secret_x"  # noqa: S105
    thread = BlockingThread()
    thread.start()
    adapter._ws_thread = thread
    rest = FakeRestClient()
    adapter._rest_client = rest
    try:
        await adapter.send(
            "ou_user_1",
            "\u5728\u7ebf agent\uff0c\u70b9\u4e00\u4e2a\u5207\u6362\uff1a",
            buttons=[
                ("405 Ava \u8d1f\u8d23\u4eba", "/switch 405"),
                ("\u961f\u5217", "notice:list"),
            ],
        )
    finally:
        thread.release()
        thread.join(timeout=2)
    assert len(rest.created) == 1
    request = rest.created[0]
    assert request.request_body.msg_type == "interactive"
    card = json.loads(request.request_body.content)
    actions = card["elements"][1]["actions"]
    assert [a["text"]["content"] for a in actions] == ["405 Ava \u8d1f\u8d23\u4eba", "\u961f\u5217"]
    assert [a["value"]["key"] for a in actions] == ["/switch 405", "notice:list"]


async def test_card_action_forwards_command_to_core(adapter: FeishuAdapter) -> None:
    """A card button tap lands in core as the command text from the operator."""
    event = SimpleNamespace(
        event=SimpleNamespace(
            operator=SimpleNamespace(open_id="ou_user_1"),
            action=SimpleNamespace(value={"key": "/switch 405"}),
        )
    )
    await adapter._handle_card_action(event)
    assert len(adapter.core.received) == 1
    message = adapter.core.received[0]
    assert message.channel == "feishu"
    assert message.chat_id == "ou_user_1"
    assert message.text == "/switch 405"
    assert adapter._last_open_id == "ou_user_1"


async def test_card_action_missing_key_ignored(adapter: FeishuAdapter) -> None:
    event = SimpleNamespace(
        event=SimpleNamespace(
            operator=SimpleNamespace(open_id="ou_user_1"),
            action=SimpleNamespace(value={}),
        )
    )
    await adapter._handle_card_action(event)
    assert adapter.core.received == []


# -- polling fallback (2026-09-01: platform delivers no receive_v1) ---------


def make_list_item(
    *,
    message_id: str | None,
    content: str = '{"text": "hello poll"}',
    open_id: str = "ou_user_1",
    sender_type: str = "user",
    msg_type: str = "text",
    chat_type: str = "p2p",
) -> SimpleNamespace:
    """A duck-typed stand-in for a listed Message (ListMessage response)."""
    return SimpleNamespace(
        message_id=message_id,
        chat_type=chat_type,
        msg_type=msg_type,
        body=SimpleNamespace(content=content),
        sender=SimpleNamespace(
            sender_type=sender_type,
            sender_id=SimpleNamespace(open_id=open_id),
        ),
        create_time="1788000000000",
    )


def make_list_item_listapi(
    *,
    message_id: str,
    content: str = '{"text": "hello poll"}',
    sender_id: str = "ou_user_1",
    id_type: str = "open_id",
    sender_type: str = "user",
    msg_type: str = "text",
) -> SimpleNamespace:
    """A ListMessage-API-shaped item: no chat_type, id on sender.id."""
    return SimpleNamespace(
        message_id=message_id,
        msg_type=msg_type,
        body=SimpleNamespace(content=content),
        sender=SimpleNamespace(
            sender_type=sender_type,
            id=sender_id,
            id_type=id_type,
        ),
        create_time="1788000000000",
    )


def make_list_response(items: list[SimpleNamespace]) -> SimpleNamespace:
    # The API returns newest-first; tests pass items in desc order explicitly.
    return SimpleNamespace(code=0, msg="ok", data=SimpleNamespace(items=items))


def poll_adapter(rest: FakeRestClient) -> FeishuAdapter:
    adapter = FeishuAdapter(FakeCore())
    adapter._rest_client = rest
    return adapter


async def test_poll_seeds_cursor_without_processing(adapter: FeishuAdapter) -> None:
    """The first poll round must NOT replay chat history (daemon restarts)."""
    rest = FakeRestClient()
    rest.list_responses = [
        make_list_response(
            [
                make_list_item(message_id="om_3"),
                make_list_item(message_id="om_2"),
                make_list_item(message_id="om_1"),
            ]
        )
    ]
    adapter._rest_client = rest
    adapter._poll_chats.add("oc_p2p_1")
    await adapter._poll_once("oc_p2p_1")
    assert adapter.core.received == []
    assert adapter._poll_cursor == {"oc_p2p_1": "om_3"}


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


def test_start_poller_honors_zero_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    """AVA_FEISHU_POLL_INTERVAL_SECONDS=0 disables the poller (WS-only)."""
    from shared.config import settings

    monkeypatch.setattr(settings.feishu, "feishu_poll_interval_seconds", 0)
    adapter = FeishuAdapter(FakeCore())
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
    adapter = FeishuAdapter(core)
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
    adapter = FeishuAdapter(core)
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
    adapter = FeishuAdapter(core)
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
