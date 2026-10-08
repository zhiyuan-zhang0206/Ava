"""Adapter rendering is frozen without tokens before durable acceptance."""

import json
from dataclasses import replace

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
        assert await adapter.outbound_account_id() == "123"
        assert prepared.chunks[0].text == "<b>hello</b> &lt;user&gt;"
        assert "TEST-TOKEN" not in prepared.model_dump_json()
        await adapter.send_prepared_outbound("42", prepared)
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
            await adapter.send_prepared_outbound("42", prepared)
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
    assert [msg.message_id for msg in core.inbound] == ["10", "10"]
    assert [msg.idempotency_key for msg in core.inbound] == [
        "telegram-callback:first",
        "telegram-callback:second",
    ]


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


async def test_notice_freezes_owner_plain_rendering_and_buttons_before_send() -> None:
    from services.entrypoints.im_bridge.outbound.types import PreparedOutboundSend

    sent: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("getMe"):
            return httpx.Response(200, json={"ok": True, "result": {"id": 123}})
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = TelegramAdapter(FakeCore(), _config(), client=client)
        buttons = (("Reply", "notice:reply:7:42"), ("Queue", "notice:list"))
        recipient, prepared = await adapter.prepare_notice_owner("**plain** <notice>", buttons)
        adapter._config = replace(adapter._config, telegram_owner_id=999)
        restored = PreparedOutboundSend.model_validate_json(prepared.model_dump_json())
        await adapter.send_prepared_outbound(recipient, restored)
    assert recipient == "42"
    assert sent == [
        {
            "chat_id": "42",
            "text": "**plain** &lt;notice&gt;",
            "parse_mode": "HTML",
            "reply_markup": {
                "inline_keyboard": [
                    [{"text": "Reply", "callback_data": "notice:reply:7:42"}],
                    [{"text": "Queue", "callback_data": "notice:list"}],
                ]
            },
        }
    ]
    assert "TEST-TOKEN" not in prepared.model_dump_json()


async def test_legacy_manifest_json_without_new_fields_still_dispatches() -> None:
    from services.entrypoints.im_bridge.outbound.types import OutboundIntent

    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("getMe"):
            return httpx.Response(200, json={"ok": True, "result": {"id": 123}})
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

    legacy = json.dumps(
        {
            "channel": "telegram",
            "chat_id": "42",
            "agent_id": 7,
            "source": {"kind": "message", "identity": "persisted", "block_idx": 0},
            "prepared": {
                "adapter_kind": "telegram-v1",
                "account_id": "123",
                "chunks": [{"text": "frozen", "fallback_text": None, "html": False}],
                "markdown": False,
                "buttons": None,
            },
            "replay_id": "",
        }
    )
    intent = OutboundIntent.model_validate_json(legacy)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = TelegramAdapter(FakeCore(), _config(), client=client)
        await adapter.send_prepared_outbound(intent.chat_id, intent.prepared)
    assert bodies == [{"chat_id": "42", "text": "frozen"}]


async def test_native_telegram_owner_freezes_plain_alert_without_buttons() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("getMe"), "preparation cannot externally send"
        return httpx.Response(200, json={"ok": True, "result": {"id": 123}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        adapter = TelegramAdapter(FakeCore(), _config(), client=client)
        target, prepared = await adapter.prepare_alert_owner("**alert** <user>")
        assert target == "42" and prepared.account_id == "123"
        assert prepared.chunks[0].text == "**alert** &lt;user&gt;"
        assert prepared.buttons == () and not prepared.markdown
        assert "TEST-TOKEN" not in prepared.model_dump_json()


async def test_native_weixin_owner_uses_login_user_and_holds_missing_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.entrypoints.im_bridge.adapters import weixin

    account = {
        "account_id": "bot",
        "user_id": "owner",
        "bot_token": "PRIVATE",
        "base_url": "https://ilink.example",
    }
    monkeypatch.setattr(weixin, "load_account", lambda: account)
    adapter = WeixinAdapter(FakeCore())
    target, prepared = await adapter.prepare_alert_owner("plain")
    assert target == "owner" and prepared.chunks[0].text == "plain"
    assert "PRIVATE" not in prepared.model_dump_json()
    account["user_id"] = ""
    with pytest.raises(SendNotStartedError, match="owner"):
        await adapter.prepare_alert_owner("plain")


async def test_native_feishu_owner_uses_last_open_id_and_holds_unknown() -> None:
    from services.entrypoints.im_bridge.adapters.feishu import FeishuAdapter
    from services.entrypoints.im_bridge.tests.slices import feishu_config

    adapter = FeishuAdapter(
        FakeCore(),
        feishu_config(
            feishu_app_id="app",
            feishu_app_secret="PRIVATE",  # noqa: S106 — mock credential
        ),
    )
    with pytest.raises(SendNotStartedError, match="owner"):
        await adapter.prepare_alert_owner("plain")
    adapter._last_open_id = "ou-owner"
    target, prepared = await adapter.prepare_alert_owner("plain")
    assert target == "ou-owner" and prepared.account_id == "app"
    assert prepared.chunks[0].text == "plain" and "PRIVATE" not in prepared.model_dump_json()
