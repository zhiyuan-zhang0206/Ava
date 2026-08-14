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
from types import SimpleNamespace
from typing import Any

import pytest

from services.im_bridge.adapters.feishu import MAX_SEGMENT_CHARS, FeishuAdapter, _segment
from services.im_bridge.core import InboundMessage


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

    @property
    def im(self) -> SimpleNamespace:
        return SimpleNamespace(v1=SimpleNamespace(message=SimpleNamespace(create=self._create)))

    def _create(self, request: Any) -> SimpleNamespace:
        self.created.append(request)
        if self.fail:
            return SimpleNamespace(success=lambda: False, code=99999, msg="denied")
        return SimpleNamespace(success=lambda: True, code=0, msg="ok")


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
    for _ in range(100):
        if adapter.core.received:
            break
        await asyncio.sleep(0.01)
    assert len(adapter.core.received) == 1


async def test_ws_callback_drops_when_no_main_loop(adapter: FeishuAdapter) -> None:
    adapter._on_im_message(make_event())
    await asyncio.sleep(0.02)
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
    ws_client = FakeWsClient()
    adapter = PatchingAdapter(FakeCore(), ws_client)
    await adapter.start()
    assert adapter._ws_thread is not None
    # lark_oapi.ws.client takes ~3s to import on a cold process (protobuf + websocket chain) before the fake's start() runs — wait well past that.
    assert ws_client.started.wait(timeout=15)
    assert adapter._ws_client is ws_client
    # Point stop() at the live pytest loop so the scheduled disconnect actually
    # executes (the fake's ws loop never runs); it must return without raising.
    adapter._ws_loop = asyncio.get_running_loop()
    await asyncio.wait_for(adapter.stop(), timeout=2)
    for _ in range(100):
        if ws_client.disconnected.is_set():
            break
        await asyncio.sleep(0.01)
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
