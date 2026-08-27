"""Unit tests for services/im_bridge/adapters/weixin.py.

Covers the contract surface: getUpdates messages reach core.handle_inbound
(and their context_token is cached to disk), sendmessage echoes the cached
context_token with the iLink headers, long texts segment at 2000 chars, stale
sessions retry once without the token, HTTP failures surface as sanitized
RuntimeErrors, an unconfigured adapter skips start() without crashing, and the
QR login flow persists credentials (chmod 600).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from services.im_bridge.adapters.weixin import (
    InboundMessage,
    WeixinAdapter,
    qr_login,
)
from shared.config import settings


class FakeCore:
    """Records inbound messages; signals when one arrives."""

    def __init__(self) -> None:
        self.inbound: list[InboundMessage] = []
        self.received = asyncio.Event()

    async def handle_inbound(self, msg: InboundMessage) -> None:
        self.inbound.append(msg)
        self.received.set()


def _message(
    *,
    text: str,
    from_user_id: str = "peer-1",
    message_id: str = "msg-1",
    context_token: str | None = "tok-ctx-1",  # noqa: S107  (test fixture value)
    message_type: int = 1,
    room_id: str | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "from_user_id": from_user_id,
        "to_user_id": "bot-id",
        "message_id": message_id,
        "message_type": message_type,
        "item_list": [{"type": 1, "text_item": {"text": text}}],
    }
    if context_token is not None:
        message["context_token"] = context_token
    if room_id is not None:
        message["room_id"] = room_id
    return message


def _updates(*messages: dict[str, Any], sync_buf: str = "buf-1") -> dict[str, Any]:
    return {"ret": 0, "msgs": list(messages), "get_updates_buf": sync_buf}


def _transport(
    script: list[httpx.Response],
) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    """MockTransport that records requests and replays ``script`` responses."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if script:
            return script.pop(0)
        return httpx.Response(200, json=_updates())

    return httpx.MockTransport(handler), captured


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Path:
    """Point the state dir at a tmp AVA_HOME and write a test account."""
    monkeypatch.setattr(settings.general, "ava_home", tmp_path)
    account = {
        "account_id": "bot-id",
        "bot_token": "test-bot-token",
        "user_id": "bot-user-id",
        "base_url": "https://ilinkai.weixin.qq.com",
    }
    path = tmp_path / "state" / "im_bridge" / "weixin_account.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(account))
    return tmp_path


def _state_file(tmp_path: Any, name: str) -> Path:
    return tmp_path / "state" / "im_bridge" / name


async def test_poll_forwards_message_and_caches_context_token(env: Any, tmp_path: Any) -> None:
    """A user text message reaches core; context_token caches and sync buf persists."""
    core = FakeCore()
    transport, _captured = _transport(
        [httpx.Response(200, json=_updates(_message(text="\u4f60\u597d", context_token="tok-abc")))]  # noqa: S106  (test fixture value)
    )
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = WeixinAdapter(core, client=client)
        await adapter.start()
        await asyncio.wait_for(core.received.wait(), timeout=2)
        await adapter.stop()

    assert core.inbound == [
        InboundMessage(channel="weixin", chat_id="peer-1", text="\u4f60\u597d", message_id="msg-1")
    ]
    assert adapter._tokens.get("peer-1") == "tok-abc"
    tokens = json.loads(_state_file(tmp_path, "weixin_context_tokens.json").read_text())
    assert tokens == {"peer-1": "tok-abc"}
    sync = json.loads(_state_file(tmp_path, "weixin_sync.json").read_text())
    assert sync["get_updates_buf"] == "buf-1"


async def test_send_echoes_context_token_and_headers(env: Any) -> None:
    """sendmessage carries the peer's cached context_token and the iLink headers."""
    transport, captured = _transport([httpx.Response(200, json={"ret": 0})])
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = WeixinAdapter(FakeCore(), client=client)
        adapter._tokens.set("peer-1", "tok-ctx-9")
        await adapter.send("peer-1", "hi")

    (request,) = captured
    msg = json.loads(request.content)["msg"]
    assert msg["to_user_id"] == "peer-1"
    assert msg["context_token"] == "tok-ctx-9"  # noqa: S105  (test fixture value)
    assert msg["item_list"][0]["text_item"]["text"] == "hi"
    assert request.url.path == "/ilink/bot/sendmessage"
    assert request.headers["Authorization"] == "Bearer test-bot-token"
    assert request.headers["AuthorizationType"] == "ilink_bot_token"
    assert request.headers["iLink-App-Id"] == "bot"
    assert request.headers["iLink-App-ClientVersion"]
    assert request.headers["X-WECHAT-UIN"]


async def test_send_splits_long_text(env: Any) -> None:
    """Text longer than 2000 chars arrives in consecutive 2000-char chunks."""
    transport, captured = _transport([httpx.Response(200, json={"ret": 0}) for _ in range(3)])
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = WeixinAdapter(FakeCore(), client=client)
        adapter._chunk_delay_seconds = 0
        await adapter.send("peer-1", "x" * 4500)

    bodies = [json.loads(r.content)["msg"] for r in captured]
    texts = [b["item_list"][0]["text_item"]["text"] for b in bodies]
    assert texts == ["x" * 2000, "x" * 2000, "x" * 500]
    assert all(b["to_user_id"] == "peer-1" for b in bodies)


async def test_session_expired_retries_without_token(env: Any) -> None:
    """errcode -14 -> one retry without the token, then success."""
    transport, captured = _transport(
        [
            httpx.Response(200, json={"ret": -14, "errmsg": "session expired"}),
            httpx.Response(200, json={"ret": 0}),
        ]
    )
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = WeixinAdapter(FakeCore(), client=client)
        adapter._tokens.set("peer-1", "stale-tok")
        await adapter.send("peer-1", "hi")

    bodies = [json.loads(r.content)["msg"] for r in captured]
    assert bodies[0]["context_token"] == "stale-tok"  # noqa: S105  (test fixture value)
    assert "context_token" not in bodies[1]
    assert adapter._tokens.get("peer-1") is None


@pytest.mark.parametrize(
    "errmsg",
    ["prepare failed", "unknown error", "", None],
)
async def test_stale_errmsg_retries_without_token(env: Any, errmsg: str | None) -> None:
    """ret=-2 with a stale-session errmsg (or none) -> one tokenless retry.

    iLink reports an expired context_token ambiguously: "prepare failed",
    "unknown error", or an empty errmsg. All three must trigger the same
    degraded retry path as errcode -14, otherwise outbound pushes after a
    long idle fail forever.
    """
    transport, captured = _transport(
        [
            httpx.Response(200, json={"ret": -2, "errmsg": errmsg}),
            httpx.Response(200, json={"ret": 0}),
        ]
    )
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = WeixinAdapter(FakeCore(), client=client)
        adapter._tokens.set("peer-1", "stale-tok")
        await adapter.send("peer-1", "hi")

    bodies = [json.loads(r.content)["msg"] for r in captured]
    assert bodies[0]["context_token"] == "stale-tok"  # noqa: S105  (test fixture value)
    assert "context_token" not in bodies[1]
    assert adapter._tokens.get("peer-1") is None


async def test_rate_limit_errmsg_not_stale(env: Any) -> None:
    """ret=-2 with a populated rate-limit errmsg is NOT a stale session.

    A genuine rate limit must keep raising so the caller sees the failure
    instead of burning the token on a pointless retry.
    """
    transport, captured = _transport(
        [httpx.Response(200, json={"ret": -2, "errmsg": "frequency limit"})]
    )
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = WeixinAdapter(FakeCore(), client=client)
        adapter._tokens.set("peer-1", "tok")
        with pytest.raises(RuntimeError, match="frequency limit"):
            await adapter.send("peer-1", "hi")

    bodies = [json.loads(r.content)["msg"] for r in captured]
    assert len(bodies) == 1  # no tokenless retry
    assert bodies[0]["context_token"] == "tok"  # noqa: S105  (test fixture value)


async def test_send_sanitizes_http_error(env: Any) -> None:
    """A non-200 send raises a RuntimeError without the token or the URL."""
    transport, _captured = _transport([httpx.Response(500, text="boom")])
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = WeixinAdapter(FakeCore(), client=client)
        with pytest.raises(RuntimeError, match="HTTP 500") as exc_info:
            await adapter.send("peer-1", "hello")
    assert "test-bot-token" not in str(exc_info.value)
    assert "ilinkai.weixin.qq.com" not in str(exc_info.value)


async def test_transport_error_sanitized(env: Any) -> None:
    """httpx transport errors surface as the exception type, never the URL."""

    def boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("https://ilinkai.weixin.qq.com/ilink/bot/sendmessage")

    async with httpx.AsyncClient(transport=httpx.MockTransport(boom)) as client:
        adapter = WeixinAdapter(FakeCore(), client=client)
        with pytest.raises(RuntimeError, match="ConnectError") as exc_info:
            await adapter.send("peer-1", "hello")
    assert "ilinkai.weixin.qq.com" not in str(exc_info.value)


async def test_skips_echo_group_bot_and_textless(env: Any) -> None:
    """Own echoes, group events, bot-type messages and textless media are dropped."""
    core = FakeCore()
    transport, _captured = _transport([])
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = WeixinAdapter(core, client=client)
        await adapter._handle_message(_message(text="echo", from_user_id="bot-id"))
        await adapter._handle_message(_message(text="group", room_id="room-1"))
        await adapter._handle_message(_message(text="bot msg", message_type=2))
        await adapter._handle_message(
            {
                "from_user_id": "peer-1",
                "message_id": "m9",
                "message_type": 1,
                "item_list": [{"type": 2, "image_item": {"media": {"url": "x"}}}],
            }
        )
    assert core.inbound == []


async def test_unconfigured_start_skips(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """No account file -> start() does nothing and send() raises."""
    monkeypatch.setattr(settings.general, "ava_home", tmp_path)
    adapter = WeixinAdapter(FakeCore())
    assert not adapter._configured
    await adapter.start()
    assert adapter._poll_task is None
    with pytest.raises(RuntimeError, match="not configured"):
        await adapter.send("peer-1", "hi")


async def test_qr_login_saves_account(env: Any, tmp_path: Any) -> None:
    """QR flow: fetch QR, poll status until confirmed, persist credentials."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "get_bot_qrcode" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "qrcode": "hex-token",
                    "qrcode_img_content": "https://wx.qq.com/scan-me",
                },
            )
        if "get_qrcode_status" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "status": "confirmed",
                    "ilink_bot_id": "bot-42",
                    "bot_token": "tok-42",
                    "ilink_user_id": "user-42",
                    "baseurl": "https://ilinkai.weixin.qq.com",
                },
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        creds = await qr_login(client=client, timeout_seconds=30)

    assert creds == {
        "account_id": "bot-42",
        "bot_token": "tok-42",
        "base_url": "https://ilinkai.weixin.qq.com",
        "user_id": "user-42",
    }
    account = _state_file(tmp_path, "weixin_account.json")
    saved = json.loads(account.read_text())
    assert saved["bot_token"] == "tok-42"  # noqa: S105  (test fixture value)
    assert saved["account_id"] == "bot-42"
    assert (account.stat().st_mode & 0o777) == 0o600


async def test_message_from_owner_is_delivered(env: Any, tmp_path: Any) -> None:
    """The owner's own WeChat id (the scanned account's user_id) is NOT the bot:
    their DMs must reach the core. Regression — was dropped as 'self' because
    the filter compared against user_id (the human) instead of account_id (the bot)."""
    core = FakeCore()
    transport, _captured = _transport(
        [httpx.Response(200, json=_updates(_message(text="hi", from_user_id="bot-user-id")))]
    )
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = WeixinAdapter(core, client=client)
        await adapter.start()
        await asyncio.wait_for(core.received.wait(), timeout=2)
        await adapter.stop()

    assert core.inbound == [
        InboundMessage(channel="weixin", chat_id="bot-user-id", text="hi", message_id="msg-1")
    ]


async def test_message_from_bot_itself_is_dropped(env: Any) -> None:
    """Messages whose from_user_id is the bot's own account_id never reach the core."""
    core = FakeCore()
    transport, _captured = _transport([])
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = WeixinAdapter(core, client=client)
        await adapter._handle_message(_message(text="echo", from_user_id="bot-id"))
    assert core.inbound == []


async def test_bot_type_message_dropped_by_message_type(env: Any) -> None:
    """message_type=2 (bot-originated) is filtered — the real field name, not msg_type."""
    core = FakeCore()
    transport, _captured = _transport([])
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = WeixinAdapter(core, client=client)
        await adapter._handle_message(_message(text="bot reply", message_type=2))
    assert core.inbound == []


# -- 24h window reminder ---------------------------------------------------


async def test_inbound_message_marks_activity(env: Any) -> None:
    """A handled inbound message records last_inbound and persists it."""
    core = FakeCore()
    transport, _captured = _transport([])
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = WeixinAdapter(core, client=client)
        assert adapter._last_inbound == {}
        await adapter._handle_message(_message(text="hi", from_user_id="peer-1"))
    assert "peer-1" in adapter._last_inbound
    data = json.loads(_state_file(env, "weixin_activity.json").read_text(encoding="utf-8"))
    assert "peer-1" in data["last_inbound"]


async def test_state_files_written_0600(env: Any, tmp_path: Any) -> None:
    """Audit round-2 P1-1: every file `_atomic_write_json` writes (context
    tokens, sync buffer, activity, account) is owner-only — the pre-fix
    production state had weixin_context_tokens.json / weixin_sync.json at
    0644 while weixin_account.json was 0600."""
    core = FakeCore()
    transport, _captured = _transport(
        [
            httpx.Response(
                200,
                json=_updates(
                    _message(text="hi", context_token="tok-abc"),  # noqa: S106 (test fixture value)
                    sync_buf="buf-2",
                ),
            )
        ]
    )
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = WeixinAdapter(core, client=client)
        await adapter.start()
        await asyncio.wait_for(core.received.wait(), timeout=2)
        await adapter.stop()

    # weixin_account.json is covered by the QR-login test below (written via
    # save_account); here the adapter itself wrote the other three.
    for name in ("weixin_context_tokens.json", "weixin_sync.json", "weixin_activity.json"):
        path = _state_file(tmp_path, name)
        assert path.is_file(), name
        assert (path.stat().st_mode & 0o777) == 0o600, name


async def test_push_failures_counted_and_reset(env: Any) -> None:
    """Task #829: consecutive sendmessage failures increment the watchdog
    counter; a success resets it and records the recovery moment."""
    transport, _captured = _transport(
        [
            # send 1: token attempt fails (stale) -> tokenless retry also fails
            httpx.Response(200, json={"ret": -2, "errmsg": "prepare failed"}),
            httpx.Response(200, json={"ret": -2, "errmsg": "prepare failed"}),
            # send 2 (token already dropped): tokenless fails
            httpx.Response(200, json={"ret": -2, "errmsg": "prepare failed"}),
            # send 3: success
            httpx.Response(200, json={"ret": 0, "message_id": "m1"}),
        ]
    )
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = WeixinAdapter(FakeCore(), client=client)
        adapter._configured = True
        adapter._user_id = "owner-1"
        adapter._tokens.set("owner-1", "ctx-token")  # token exists -> stale retry path
        with pytest.raises(RuntimeError):
            await adapter.send("owner-1", "hello")
        # one failed send call (token attempt + tokenless retry) = 1 failure
        assert adapter.push_failures == 1
        assert adapter.push_failed_at is not None
        with pytest.raises(RuntimeError):
            await adapter.send("owner-1", "still broken")
        assert adapter.push_failures == 2  # consecutive, now past the threshold
        await adapter.send("owner-1", "again")
        assert adapter.push_failures == 0
        assert adapter.push_recovered_at is not None
