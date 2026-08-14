"""`services.im_bridge.adapters.telegram` — poll wiring, tap handling, edits.

Regression guard: inline-keyboard taps arrive as ``callback_query`` updates —
``allowed_updates`` must list them or Telegram silently drops every button
press (the /list tap-to-switch card was dead in prod, 2026-08-03).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from services.im_bridge.adapters.telegram import TelegramAdapter
from shared.config import settings

FAKE_BOT_TOKEN = "123456:TEST-TOKEN"  # noqa: S105 - test fixture, never a real secret
OWNER = 123456789


class _FakeCore:
    """Records what the adapter hands over; replies immediately."""

    def __init__(self) -> None:
        self.inbound: list[tuple[str, str, str]] = []  # (channel, chat_id, text)

    async def handle_inbound(self, msg: Any) -> None:
        self.inbound.append((msg.channel, msg.chat_id, msg.text))


def _adapter(core: _FakeCore, handler: Any) -> TelegramAdapter:
    return TelegramAdapter(core, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def _settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.setattr(settings.telegram, "telegram_bot_token", FAKE_BOT_TOKEN)
    monkeypatch.setattr(settings.telegram, "telegram_owner_id", OWNER)
    monkeypatch.setattr(settings.general, "ava_home", str(tmp_path))


def test_get_updates_requests_callback_query(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """The poll asks Telegram for message AND callback_query updates — without
    the latter, inline-keyboard taps never arrive (prod bug 2026-08-03)."""
    _settings(monkeypatch, tmp_path)
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"ok": True, "result": []})

    async def scenario() -> None:
        adapter = _adapter(_FakeCore(), handler)
        await adapter._get_updates()
        await adapter._http.aclose()

    asyncio.run(scenario())
    allowed = json.loads(seen["params"]["allowed_updates"])
    assert allowed == ["message", "callback_query"]


def test_get_updates_uses_configured_poll_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """The long-poll window rides AVA_TELEGRAM_POLL_TIMEOUT_SECONDS (task
    #698 G8), and the HTTP read timeout stays 10s above it."""
    _settings(monkeypatch, tmp_path)
    monkeypatch.setattr(settings.telegram, "telegram_poll_timeout_seconds", 33)
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["timeout_param"] = request.url.params.get("timeout")
        return httpx.Response(200, json={"ok": True, "result": []})

    async def scenario() -> None:
        adapter = _adapter(_FakeCore(), handler)
        await adapter._get_updates()
        await adapter._http.aclose()

    asyncio.run(scenario())
    assert seen["timeout_param"] == "33"


def test_callback_tap_runs_command_and_acks(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """A button tap becomes a command in the core, then the tap is acked so
    Telegram clears the spinner."""
    _settings(monkeypatch, tmp_path)
    core = _FakeCore()
    answered: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/answerCallbackQuery"):
            answered.append(json.loads(request.content)["callback_query_id"])
            return httpx.Response(200, json={"ok": True, "result": True})
        raise AssertionError(f"unexpected call: {request.url}")

    update = {
        "update_id": 42,
        "callback_query": {
            "id": "cq-1",
            "from": {"id": OWNER},
            "message": {
                "message_id": 77,
                "chat": {"id": OWNER},
                "text": "Live agents",
            },
            "data": "/switch 405",
        },
    }

    async def scenario() -> None:
        adapter = _adapter(core, handler)
        await adapter._handle_update(update)
        await adapter._http.aclose()

    asyncio.run(scenario())
    assert core.inbound == [("telegram", str(OWNER), "/switch 405")]
    assert answered == ["cq-1"]


def test_callback_from_stranger_consumed(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    _settings(monkeypatch, tmp_path)
    core = _FakeCore()
    answered: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/answerCallbackQuery"):
            answered.append("called")
            return httpx.Response(200, json={"ok": True, "result": True})
        raise AssertionError(f"unexpected call: {request.url}")

    update = {
        "update_id": 43,
        "callback_query": {
            "id": "cq-2",
            "from": {"id": 999},
            "message": {"message_id": 78, "chat": {"id": 999}},
            "data": "/switch 405",
        },
    }

    async def scenario() -> None:
        adapter = _adapter(core, handler)
        await adapter._handle_update(update)
        await adapter._http.aclose()

    asyncio.run(scenario())
    assert core.inbound == []  # stranger taps are consumed, never surfaced
    assert answered == []  # and never acked


def test_send_posts_html_message(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    _settings(monkeypatch, tmp_path)
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1234}})

    async def scenario() -> None:
        adapter = _adapter(_FakeCore(), handler)
        await adapter.send(str(OWNER), "hello")
        await adapter._http.aclose()

    asyncio.run(scenario())
    assert captured["payload"]["text"] == "hello"
    assert captured["payload"]["parse_mode"] == "HTML"  # send always renders escaped HTML


def test_typing_sends_chat_action(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """The native processing indicator: sendChatAction with action=typing —
    Telegram shows the bouncing dots, no message is sent."""
    _settings(monkeypatch, tmp_path)
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "result": True})

    async def scenario() -> None:
        adapter = _adapter(_FakeCore(), handler)
        await adapter.typing(str(OWNER))
        await adapter._http.aclose()

    asyncio.run(scenario())
    assert captured["payload"] == {"chat_id": str(OWNER), "action": "typing"}


def test_command_menu_includes_spawn_and_commands(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """The persistent command menu registers the full command set — tapping
    one autofills the input box (Telegram native behavior)."""
    _settings(monkeypatch, tmp_path)
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "result": True})

    async def scenario() -> None:
        adapter = _adapter(_FakeCore(), handler)
        await adapter._install_command_menu()
        await adapter._http.aclose()

    asyncio.run(scenario())
    commands = [c["command"] for c in captured["payload"]["commands"]]
    # /switch is gone from the frontend: /list buttons switch (user ruling 2026-08-04)
    assert commands == ["list", "spawn", "status", "commands", "help"]
