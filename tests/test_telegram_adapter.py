"""Unit tests for services/im_bridge/adapters/telegram.py.

Covers the contract surface: owner messages reach core.handle_inbound, strangers
are consumed but ignored, the getUpdates offset persists atomically and is
reused, long texts segment at 4096 chars, and HTTP failures surface as
sanitized RuntimeErrors (no bot token in the message).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from services.im_bridge.adapters.telegram import InboundMessage, TelegramAdapter
from shared.config import settings


class FakeCore:
    """Records inbound messages; signals when one arrives."""

    def __init__(self) -> None:
        self.inbound: list[InboundMessage] = []
        self.received = asyncio.Event()

    async def handle_inbound(self, msg: InboundMessage) -> None:
        self.inbound.append(msg)
        self.received.set()


def _update(
    update_id: int,
    chat_id: int,
    *,
    text: str | None = None,
    caption: str | None = None,
    message_id: int = 1,
) -> dict[str, Any]:
    """One Telegram update object; media is simulated by adding a photo key."""
    message: dict[str, Any] = {
        "message_id": message_id,
        "date": 1,
        "chat": {"id": chat_id},
    }
    if text is not None:
        message["text"] = text
    if caption is not None:
        message["caption"] = caption
    if caption is not None:
        message["photo"] = [{"file_id": "x"}]  # media content the adapter ignores
    return {"update_id": update_id, "message": message}


def _transport(
    script: list[httpx.Response],
) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    """MockTransport that records requests and replays ``script`` responses.

    Once the script is exhausted, getUpdates answers with an empty batch (the
    long-poll equivalent of "nothing new") instead of failing.
    """
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if script:
            return script.pop(0)
        return httpx.Response(200, json={"ok": True, "result": []})

    return httpx.MockTransport(handler), captured


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """Point the adapter's settings at a test token/owner and a tmp AVA_HOME."""
    monkeypatch.setattr(settings.telegram, "telegram_bot_token", "123456:TEST-TOKEN")
    monkeypatch.setattr(settings.telegram, "telegram_owner_id", 42)
    monkeypatch.setattr(settings.general, "ava_home", tmp_path)


async def _wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_poll(), timeout=timeout)


def _offset_file(tmp_path: Any) -> Any:
    return tmp_path / "state" / "im_bridge" / "telegram_offset"


async def test_poll_loop_forwards_owner_text(env: None, tmp_path: Any) -> None:
    """Owner text message -> InboundMessage reaches core; offset advances."""
    core = FakeCore()
    transport, captured = _transport(
        [
            httpx.Response(200, json={"ok": True, "result": True}),  # setMyCommands
            httpx.Response(
                200,
                json={"ok": True, "result": [_update(5, 42, text="hello", message_id=9)]},
            ),
        ]
    )
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = TelegramAdapter(core, client=client)
        try:
            await adapter.start()
            await asyncio.wait_for(core.received.wait(), timeout=2)
        finally:
            await adapter.stop()

    assert core.inbound == [
        InboundMessage(channel="telegram", chat_id="42", text="hello", message_id="9")
    ]
    # First getUpdates used offset 0 (no file yet); the persisted offset is 6.
    first = next(r for r in captured if "getUpdates" in str(r.url))
    assert int(first.url.params["offset"]) == 0
    assert _offset_file(tmp_path).read_text() == "6"


async def test_non_owner_ignored_but_offset_advances(env: None, tmp_path: Any) -> None:
    """A stranger's message is consumed (offset moves) but never forwarded."""
    core = FakeCore()
    transport, _captured = _transport(
        [
            httpx.Response(200, json={"ok": True, "result": True}),  # setMyCommands
            httpx.Response(
                200,
                json={"ok": True, "result": [_update(7, 99, text="who are you")]},
            ),
        ]
    )
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = TelegramAdapter(core, client=client)
        try:
            await adapter.start()
            # poll loop consumes the update; the ack lands after handling
            await _wait_until(lambda: _offset_file(tmp_path).exists(), timeout=2)
        finally:
            await adapter.stop()

    assert core.inbound == []
    assert _offset_file(tmp_path).read_text() == "8"


async def test_offset_persisted_and_reused(env: None, tmp_path: Any) -> None:
    """The next long poll sends the persisted offset, not 0."""
    core = FakeCore()
    transport, captured = _transport(
        [
            httpx.Response(200, json={"ok": True, "result": True}),  # setMyCommands
            httpx.Response(
                200,
                json={"ok": True, "result": [_update(7, 42, text="hi")]},
            ),
            httpx.Response(200, json={"ok": True, "result": []}),
        ]
    )
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = TelegramAdapter(core, client=client)
        try:
            await adapter.start()
            await asyncio.wait_for(core.received.wait(), timeout=2)
            await _wait_until(lambda: len(captured) >= 3)  # setMyCommands + 2 getUpdates
        finally:
            await adapter.stop()

    offsets = [int(r.url.params["offset"]) for r in captured if "getUpdates" in str(r.url)]
    assert offsets == [0, 8]
    assert _offset_file(tmp_path).read_text() == "8"


async def test_send_splits_long_text(env: None) -> None:
    """Text longer than 4096 chars is sent in consecutive 4096-char chunks."""
    transport, captured = _transport(
        [
            httpx.Response(200, json={"ok": True, "result": {"message_id": 1}}),
            httpx.Response(200, json={"ok": True, "result": {"message_id": 2}}),
            httpx.Response(200, json={"ok": True, "result": {"message_id": 3}}),
        ]
    )
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = TelegramAdapter(FakeCore(), client=client)
        await adapter.send("42", "x" * 9000)

    bodies = [json.loads(r.content) for r in captured]
    assert [b["text"] for b in bodies] == ["x" * 4096, "x" * 4096, "x" * 808]
    assert all(b["chat_id"] == "42" for b in bodies)
    assert all("sendMessage" in str(r.url) for r in captured)


async def test_send_sanitizes_http_error(env: None) -> None:
    """A non-200 send raises a RuntimeError without the token or the URL."""
    transport, _captured = _transport(
        [httpx.Response(401, json={"ok": False, "description": "Unauthorized"})]
    )
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = TelegramAdapter(FakeCore(), client=client)
        with pytest.raises(RuntimeError, match="HTTP 401") as exc_info:
            await adapter.send("42", "hello")

    # The traceback must not leak the token (httpx exceptions embed the URL).
    assert "TEST-TOKEN" not in str(exc_info.value)
    assert "api.telegram.org" not in str(exc_info.value)


async def test_getupdates_non_200_sanitized(env: None) -> None:
    """A non-200 getUpdates raises a sanitized RuntimeError (no token)."""
    transport, _captured = _transport([httpx.Response(401, json={"ok": False})])
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = TelegramAdapter(FakeCore(), client=client)
        with pytest.raises(RuntimeError, match="HTTP 401"):
            await adapter._get_updates()


async def test_getupdates_transport_error_sanitized(env: None) -> None:
    """httpx transport errors surface as the exception type, never the URL."""

    def boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("https://api.telegram.org/bot123456:TEST-TOKEN/getUpdates")

    async with httpx.AsyncClient(transport=httpx.MockTransport(boom)) as client:
        adapter = TelegramAdapter(FakeCore(), client=client)
        with pytest.raises(RuntimeError, match="ConnectError") as exc_info:
            await adapter._get_updates()
    assert "TEST-TOKEN" not in str(exc_info.value)
    assert "api.telegram.org" not in str(exc_info.value)


async def test_media_caption_forwarded_textless_ignored(env: None) -> None:
    """Media with a caption forwards the caption; textless media is ignored."""
    core = FakeCore()
    transport, _captured = _transport([])
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = TelegramAdapter(core, client=client)
        await adapter._handle_update(_update(1, 42, caption="see this"))
        await adapter._handle_update(_update(2, 42, message_id=3))  # photo, no caption
        await adapter._handle_update({"update_id": 3, "edited_message": {"chat": {"id": 42}}})

    assert core.inbound == [
        InboundMessage(channel="telegram", chat_id="42", text="see this", message_id="1")
    ]


async def test_start_skips_when_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """No token / no owner id -> start() logs and returns (the daemon stays
    up either way, like the weixin/feishu adapters) — a fresh install with no
    bot token must not kill the im-bridge session."""
    monkeypatch.setattr(settings.telegram, "telegram_bot_token", "")
    monkeypatch.setattr(settings.telegram, "telegram_owner_id", 0)
    adapter = TelegramAdapter(FakeCore())
    await adapter.start()
    assert adapter._poll_task is None


# --- v2: HTML rendering / buttons / callback taps / command menu ---


def _send_response() -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})


async def test_send_escapes_html_and_marks_up_agent_content(env: Any) -> None:
    """markdown=True renders a light markdown subset to HTML; every outgoing
    text is HTML-escaped first so user data cannot inject tags."""
    transport, captured = _transport([_send_response()])
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = TelegramAdapter(FakeCore(), client=client)
        await adapter.send(
            "42",
            "hello <b>world</b> & **bold** `code` [link](https://x.com/a)",
            markdown=True,
        )
    (request,) = captured
    body = json.loads(request.content)
    assert body["parse_mode"] == "HTML"
    assert body["text"] == (
        "hello &lt;b&gt;world&lt;/b&gt; &amp; <b>bold</b> <code>code</code> "
        '<a href="https://x.com/a">link</a>'
    )
    assert "reply_markup" not in body


async def test_send_plain_text_is_escaped(env: Any) -> None:
    """Command replies (markdown=False) are escaped so stray < > & are safe."""
    transport, captured = _transport([_send_response()])
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = TelegramAdapter(FakeCore(), client=client)
        await adapter.send("42", "a < b & c > d")
    (request,) = captured
    assert json.loads(request.content)["text"] == "a &lt; b &amp; c &gt; d"


async def test_send_attaches_inline_keyboard_buttons(env: Any) -> None:
    """buttons render as an inline keyboard whose callback_data carries the
    command — one button per row."""
    transport, captured = _transport([_send_response()])
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = TelegramAdapter(FakeCore(), client=client)
        await adapter.send(
            "42",
            "\u9009\u62e9 agent",
            buttons=[("405 Ava", "/switch 405"), ("228 CEO", "/switch 228")],
        )
    (request,) = captured
    markup = json.loads(request.content)["reply_markup"]
    assert markup == {
        "inline_keyboard": [
            [{"text": "405 Ava", "callback_data": "/switch 405"}],
            [{"text": "228 CEO", "callback_data": "/switch 228"}],
        ]
    }


async def test_send_falls_back_to_plain_text_when_html_rejected(env: Any) -> None:
    """A 400 on the HTML attempt resends the same text without parse_mode —
    formatting is lost, content is not."""
    transport, captured = _transport(
        [
            httpx.Response(400, json={"ok": False, "description": "can't parse entities"}),
            _send_response(),
        ]
    )
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = TelegramAdapter(FakeCore(), client=client)
        await adapter.send("42", "**bold**", markdown=True)
    assert len(captured) == 2
    first, second = (json.loads(r.content) for r in captured)
    assert first["parse_mode"] == "HTML"
    assert second["text"] == "**bold**"  # original text, no entities
    assert "parse_mode" not in second


def _callback_update(
    update_id: int,
    callback_id: str,
    chat_id: int,
    data: str,
    message_id: int = 7,
) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": callback_id,
            "from": {"id": chat_id, "is_bot": False, "first_name": "John"},
            "message": {"message_id": message_id, "chat": {"id": chat_id}},
            "data": data,
        },
    }


async def test_callback_tap_runs_command_and_answers(env: Any) -> None:
    """Tapping a switch button delivers the command like a typed message and
    acknowledges the tap so Telegram clears the button spinner."""
    core = FakeCore()
    transport, captured = _transport([httpx.Response(200, json={"ok": True, "result": []})])
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = TelegramAdapter(core, client=client)
        await adapter._handle_callback(
            _callback_update(1, "cb-1", 42, "/switch 405")["callback_query"]
        )
    assert core.inbound == [
        InboundMessage(channel="telegram", chat_id="42", text="/switch 405", message_id="7")
    ]
    (request,) = captured
    assert request.url.path.endswith("/answerCallbackQuery")
    assert json.loads(request.content)["callback_query_id"] == "cb-1"


async def test_start_installs_command_menu(env: Any) -> None:
    """start() sets the persistent command menu (setMyCommands); failure is
    tolerated (the poll loop still runs)."""
    transport, captured = _transport([httpx.Response(200, json={"ok": True, "result": True})])
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = TelegramAdapter(FakeCore(), client=client)
        await adapter.start()
        try:
            (request,) = captured
            assert request.url.path.endswith("/setMyCommands")
            commands = json.loads(request.content)["commands"]
            assert {c["command"] for c in commands} == {
                "list",
                "spawn",
                "status",
                "commands",
                "help",
            }
        finally:
            await adapter.stop()


# --- durability: offset advances only after delivery; no double delivery ---


class FailingCore:
    """handle_inbound raises once, then records — simulates a gateway down."""

    def __init__(self) -> None:
        self.inbound: list[InboundMessage] = []
        self.fail_first = True

    async def handle_inbound(self, msg: InboundMessage) -> None:
        if self.fail_first:
            self.fail_first = False
            raise RuntimeError("gateway down")
        self.inbound.append(msg)


async def test_unacked_update_is_refetched_after_failure(env: Any, tmp_path: Any) -> None:
    """A message whose delivery fails keeps the offset put — the next poll
    re-fetches it, so nothing is dropped in a rollout window."""
    core = FailingCore()
    transport, captured = _transport(
        [
            httpx.Response(200, json={"ok": True, "result": True}),  # setMyCommands
            httpx.Response(
                200, json={"ok": True, "result": [_update(5, 42, text="hello", message_id=9)]}
            ),
            httpx.Response(
                200, json={"ok": True, "result": [_update(5, 42, text="hello", message_id=9)]}
            ),
        ]
    )
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = TelegramAdapter(core, client=client)
        try:
            await adapter.start()
            await _wait_until(lambda: len(core.inbound) == 1, timeout=3)
        finally:
            await adapter.stop()

    assert core.inbound == [
        InboundMessage(channel="telegram", chat_id="42", text="hello", message_id="9")
    ]
    # first poll failed (no ack → offset file still 0), second poll re-fetched
    offsets = [int(r.url.params["offset"]) for r in captured if "getUpdates" in str(r.url)]
    assert offsets[0] == 0
    assert offsets[1] == 0  # unacked — re-fetched from the same offset


async def test_refetched_update_not_delivered_twice(env: Any, tmp_path: Any) -> None:
    """After a successful delivery the offset advances; if the same update
    were re-fetched anyway, the in-process dedup prevents a duplicate."""
    core = FakeCore()
    transport, _captured = _transport(
        [
            httpx.Response(200, json={"ok": True, "result": True}),  # setMyCommands
            httpx.Response(
                200, json={"ok": True, "result": [_update(5, 42, text="hi", message_id=9)]}
            ),
        ]
    )
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = TelegramAdapter(core, client=client)
        try:
            await adapter.start()
            await asyncio.wait_for(core.received.wait(), timeout=2)
        finally:
            await adapter.stop()
    assert len(core.inbound) == 1
    # simulate a re-fetch of the same update (crash before the offset write)
    await adapter._handle_update(_update(5, 42, text="hi", message_id=9))
    assert len(core.inbound) == 1  # dedup: not delivered again
