"""Adapter rendering is frozen without tokens before durable acceptance."""

import json

import httpx
import pytest

from services.entrypoints.im_bridge.adapters.telegram import TelegramAdapter
from services.entrypoints.im_bridge.adapters.tests.test_telegram_adapter import FakeCore, _config
from services.entrypoints.im_bridge.adapters.weixin import WeixinAdapter
from services.entrypoints.im_bridge.types import SendNotStartedError


async def test_telegram_getme_is_cached_and_frozen_html_uses_plain_rejection_fallback() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("getMe"):
            return httpx.Response(200, json={"ok": True, "result": {"id": 123, "is_bot": True}})
        if json.loads(request.content).get("parse_mode") == "HTML":
            return httpx.Response(400, json={"ok": False, "description": "bad entities"})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = TelegramAdapter(FakeCore(), _config(), client=client)
        prepared = await adapter.prepare_timeline("**hello** <user>")
        assert await adapter.timeline_account_id() == "123"
        assert prepared.chunks[0].text == "<b>hello</b> &lt;user&gt;"
        assert "TEST-TOKEN" not in prepared.model_dump_json()
        await adapter.send_prepared_timeline("42", prepared)
    assert len([call for call in calls if call.url.path.endswith("getMe")]) == 1
    assert json.loads(calls[-1].content) == {"chat_id": "42", "text": "**hello** <user>"}


async def test_telegram_second_chunk_connect_failure_is_uncertain_not_unstarted() -> None:
    chunks = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal chunks
        if request.url.path.endswith("getMe"):
            return httpx.Response(200, json={"ok": True, "result": {"id": 123}})
        chunks += 1
        if chunks == 2:
            raise httpx.ConnectError("URL contains TEST-TOKEN", request=request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": chunks}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = TelegramAdapter(FakeCore(), _config(), client=client)
        prepared = await adapter.prepare_timeline("x" * 4097)
        with pytest.raises(RuntimeError, match="acknowledged") as error:
            await adapter.send_prepared_timeline("42", prepared)
        assert not isinstance(error.value, SendNotStartedError)
        assert "TEST-TOKEN" not in str(error.value)
    assert chunks == 2


async def test_getme_failure_has_no_send_and_no_token_in_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("https://api.telegram.org/bot123:TEST-TOKEN/getMe", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = TelegramAdapter(FakeCore(), _config(), client=client)
        with pytest.raises(RuntimeError) as error:
            await adapter.prepare_timeline("hello")
        assert "TEST-TOKEN" not in str(error.value)


async def test_callback_invocation_uses_click_identity_not_shared_bot_message() -> None:
    core = FakeCore()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = TelegramAdapter(core, _config(), client=client)
        for click in ("first", "second"):
            await adapter._handle_callback(
                {
                    "id": click,
                    "data": "/switch 7",
                    "message": {"message_id": 10, "chat": {"id": 42}},
                }
            )
    assert [msg.message_id for msg in core.inbound] == ["callback:first", "callback:second"]


async def test_weixin_preparation_uses_existing_login_identity_not_context_or_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.entrypoints.im_bridge.adapters import weixin

    monkeypatch.setattr(
        weixin,
        "load_account",
        lambda: {
            "account_id": "bot-id",
            "user_id": "user",
            "bot_token": "PRIVATE",
            "base_url": "https://ilink.example",
        },
    )
    adapter = WeixinAdapter(FakeCore())
    adapter._tokens.set("chat", "CONTEXT")
    prepared = await adapter.prepare_timeline("hello")
    assert "bot-id" in prepared.account_id
    assert (
        "PRIVATE" not in prepared.model_dump_json() and "CONTEXT" not in prepared.model_dump_json()
    )
    adapter._base_url = "https://user:PRIVATE@ilink.example"
    with pytest.raises(ValueError, match="credentials"):
        await adapter.prepare_timeline("hello")
