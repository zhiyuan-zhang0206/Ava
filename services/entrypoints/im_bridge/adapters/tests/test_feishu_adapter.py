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

import services.entrypoints.im_bridge.adapters.feishu as feishu_module
from services.entrypoints.im_bridge.adapters.feishu import (
    MAX_SEGMENT_CHARS,
    FeishuAdapter,
    _segment,
)
from services.entrypoints.im_bridge.tests.slices import feishu_config
from services.entrypoints.im_bridge.types import InboundMessage
from tests.components.base.poll_until import poll_until_async


class FakeCore:
    def __init__(self) -> None:
        self.received: list[InboundMessage] = []
        # The core contract includes the adapter registry (types.IMAdapter), and
        # the boot owner-seed reads it to alert through telegram.
        self.adapters: dict[str, Any] = {}

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

    def __init__(self, core: FakeCore, config: Any, ws_client: FakeWsClient) -> None:
        super().__init__(core, config)
        self._ws_client_impl = ws_client

    def _build_ws_client(self) -> Any:
        return self._ws_client_impl


class _LogRecorder:
    """Captures info() calls so tests can assert delivery-count lines."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def info(self, message: str, *args: Any) -> None:
        self.messages.append(message.format(*args) if args else message)


@pytest.fixture
def adapter() -> FeishuAdapter:
    return FeishuAdapter(FakeCore(), feishu_config())


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
    await poll_until_async(lambda: bool(adapter.core.received))
    assert len(adapter.core.received) == 1


async def test_ws_callback_drops_when_no_main_loop(adapter: FeishuAdapter) -> None:
    adapter._on_im_message(make_event())
    # The no-loop path drops synchronously, so no delivery can arrive later.
    await asyncio.sleep(0.05)
    assert adapter.core.received == []


# -- credentials / lifecycle ------------------------------------------------


async def test_start_skips_without_credentials() -> None:
    adapter = FeishuAdapter(FakeCore(), feishu_config(feishu_app_id="", feishu_app_secret=""))
    await adapter.start()
    assert adapter._ws_thread is None
    assert adapter._ws_client is None


async def test_start_connects_with_credentials() -> None:
    # The ws thread's first act is a COLD import of lark_oapi.ws.client (a
    # protobuf + websocket import chain, seconds on a loaded CI runner) before
    # the fake's start() can run, so the timed wait below would race that
    # import. Warm it here — inside the running loop, so the SDK's module-level
    # asyncio.get_event_loop() binds without a deprecation path — leaving the
    # wait to cover only thread startup. Production is untouched: the adapter
    # still imports lark lazily in the ws thread (see FeishuAdapter docstring).
    import lark_oapi.ws.client  # noqa: F401  # pyright: ignore[reportUnusedImport]

    ws_client = FakeWsClient()
    credentials = feishu_config(feishu_app_id="cli_x", feishu_app_secret="secret_x")  # noqa: S106
    adapter = PatchingAdapter(FakeCore(), credentials, ws_client)
    await adapter.start()
    assert (adapter._app_id, adapter._app_secret) == ("cli_x", "secret_x")
    assert adapter._ws_thread is not None
    assert ws_client.started.wait(timeout=15)
    assert adapter._ws_client is ws_client
    # Point stop() at the live pytest loop so the scheduled disconnect actually
    # executes (the fake's ws loop never runs); it must return without raising.
    adapter._ws_loop = asyncio.get_running_loop()
    await asyncio.wait_for(adapter.stop(), timeout=30.0)
    await poll_until_async(ws_client.disconnected.is_set)
    assert ws_client.disconnected.is_set()


# -- ws proxy (issue #2089) -------------------------------------------------


def test_ws_connect_kwargs_defer_to_the_machine_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host that reaches the network only through a proxy must keep its long
    connection. The SDK pins ``proxy=None`` on websockets>=15 (its historical
    direct behavior), which turns the environment discovery off — the builder
    restores it whenever the machine names a proxy, and keeps the SDK's own
    direct behavior when it does not.

    A wss target is covered by ``HTTPS_PROXY`` (websockets' per-target discovery
    also honors ``NO_PROXY``); an ``ALL_PROXY``-only environment still connects
    directly, because that discovery maps no ``all`` scheme onto a websocket
    target. The key list is the env registry's — the same one ``child_env``
    forwards to service children.
    """
    import inspect

    import websockets

    from base.host.env.registry import NETWORK_PROXY_KEYS
    from services.entrypoints.im_bridge.adapters import feishu_ws_proxy

    for key in NETWORK_PROXY_KEYS:
        monkeypatch.delenv(key, raising=False)
    proxy_param = inspect.signature(websockets.connect).parameters.get("proxy")
    # The fix rests on websockets' "argument omitted = discover from the
    # environment" default (proxy=True). If a future websockets flips it, fail
    # here rather than letting the long connection go direct-only again.
    assert proxy_param is not None and proxy_param.default is True
    assert feishu_ws_proxy.ws_connect_kwargs() == {"proxy": None}

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897")
    assert feishu_ws_proxy.ws_connect_kwargs() == {}
    monkeypatch.delenv("HTTPS_PROXY")

    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:7897")
    assert feishu_ws_proxy.ws_connect_kwargs() == {}


def test_ws_connect_kwargs_mirror_the_sdk_version_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """websockets<15 has no ``proxy`` parameter and its ``connect()`` rejects
    unknown keyword arguments — so the builder must mirror the SDK's version
    guard and emit nothing on such an install, regardless of the environment."""
    import websockets

    from services.entrypoints.im_bridge.adapters import feishu_ws_proxy

    def legacy_connect(uri: str) -> None:
        """A pre-15 ``connect`` — no ``proxy`` parameter."""

    monkeypatch.setattr(websockets, "connect", legacy_connect)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897")
    assert feishu_ws_proxy.ws_connect_kwargs() == {}


async def test_sdk_connect_call_site_resolves_the_patched_builder() -> None:
    """The seam is a NAME contract: ``Client._connect`` resolves
    ``_ws_connect_kwargs`` as a module global at connect time, so the adapter's
    ``setattr`` lands only while the SDK keeps calling that name. A lark-oapi
    rename (a dependency bump is the realistic path) would leave the replacement
    installed but unused — silent direct-only behavior again — so fingerprint
    the call site (QA review of #2105, guard suggestion)."""
    import inspect

    # Warm the COLD lark import inside the running loop (see the sibling test).
    import lark_oapi.ws.client as ws_client_module

    source = inspect.getsource(ws_client_module.Client._connect)
    assert "_ws_connect_kwargs()" in source, (
        "lark-oapi's Client._connect no longer calls _ws_connect_kwargs(): the "
        "env-proxy seam in services/entrypoints/im_bridge/adapters/feishu_ws_proxy.py is now "
        "inert (the WS handshake is direct-only again)"
    )


async def test_build_ws_client_installs_the_env_proxy_kwargs_builder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``lark.ws.Client`` takes no proxy parameter, so the SDK's module-level
    kwargs builder is the only seam (``Client._connect`` resolves it by name at
    connect time). Building the ws client must install the env-aware builder in
    its place."""
    # Warm the COLD lark import inside the running loop, like
    # test_start_connects_with_credentials does: the SDK binds a module-level
    # event loop on its first import.
    import lark_oapi.ws.client as ws_client_module

    from services.entrypoints.im_bridge.adapters import feishu_ws_proxy

    adapter = FeishuAdapter(FakeCore(), feishu_config(feishu_rest_timeout_seconds=7.5))
    adapter._app_id = "cli_x"
    adapter._app_secret = "secret_x"  # noqa: S105 — a literal, never a real credential
    monkeypatch.setattr(ws_client_module, "_ws_connect_kwargs", lambda: {"proxy": None})

    adapter._build_ws_client()

    assert ws_client_module._ws_connect_kwargs is feishu_ws_proxy.ws_connect_kwargs
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897")
    assert ws_client_module._ws_connect_kwargs() == {}


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


async def test_send_logs_one_delivery_line_per_segment(
    adapter: FeishuAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each API-confirmed segment appends one 'feishu send ok' line (the
    delivery-count surface, task #4250); the poller-registration line is not
    part of that count."""
    adapter._app_id = "cli_x"
    adapter._app_secret = "secret_x"  # noqa: S105
    thread = BlockingThread()
    thread.start()
    adapter._ws_thread = thread
    rest = FakeRestClient()
    adapter._rest_client = rest
    recorder = _LogRecorder()
    monkeypatch.setattr(feishu_module, "logger", recorder)
    try:
        await adapter.send("ou_user_1", "a" * (MAX_SEGMENT_CHARS * 2 + 123))
    finally:
        thread.release()
        thread.join(timeout=2)
    oks = [message for message in recorder.messages if "send ok" in message]
    assert oks == ["feishu send ok chat_id=ou_user_1 message_id=om_sent_1"] * 3


async def test_card_send_logs_delivery_line(
    adapter: FeishuAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The interactive-card path logs its own 'feishu send ok' line."""
    adapter._app_id = "cli_x"
    adapter._app_secret = "secret_x"  # noqa: S105
    thread = BlockingThread()
    thread.start()
    adapter._ws_thread = thread
    rest = FakeRestClient()
    adapter._rest_client = rest
    recorder = _LogRecorder()
    monkeypatch.setattr(feishu_module, "logger", recorder)
    try:
        await adapter.send("ou_user_1", "hi", buttons=[("List", "/list")])
    finally:
        thread.release()
        thread.join(timeout=2)
    oks = [message for message in recorder.messages if "send ok" in message]
    assert oks == ["feishu send ok chat_id=ou_user_1 message_id=om_sent_1"]


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
    raises the fan-out's skip signal, NotImplementedError — not an
    AttributeError from an uninitialized _last_open_id, and not a RuntimeError
    that the notify fan-out would retry forever (task #4964)."""
    with pytest.raises(NotImplementedError, match="no known user chat"):
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
    adapter = FeishuAdapter(FakeCore(), feishu_config(feishu_rest_timeout_seconds=7.5))
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
    adapter = FeishuAdapter(FakeCore(), feishu_config())
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
