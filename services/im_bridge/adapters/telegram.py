"""IM Bridge Telegram adapter — long-poll the owner's Telegram chat via the Bot API.

One adapter per IM platform; this one speaks Telegram's Bot API directly:
``getUpdates`` long-polling (50s window, offset persisted atomically under
``$AVA_HOME/state/im_bridge``) for inbound messages, ``sendMessage`` (with
4096-char segmentation) for outbound. Only the configured owner's chat is
handled; messages from anyone else are consumed (the offset moves past them)
but never surfaced.

Error hygiene: httpx exceptions embed the request URL, which carries the bot
token — nothing from an httpx exception ever reaches a caller. Status codes
and response bodies are surfaced only (they never contain the token), so a
traceback cannot leak the token.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from pathlib import Path
from typing import Any

import httpx

from services.im_bridge.core import IMAdapter, InboundMessage
from shared.config import settings
from shared.log import logger

# Telegram's per-message cap for plain-text messages.
_MAX_MESSAGE_LEN = 4096

# The commands offered through the persistent command menu (setMyCommands).
# Tapping one autofills the input box — Telegram's native behavior, which is
# why /spawn and /commands live here too.
_COMMAND_MENU = [
    {"command": "list", "description": "Live agents (tap to switch)"},
    {"command": "spawn", "description": "Spawn an agent (menu)"},
    {"command": "status", "description": "Current agent status"},
    {"command": "commands", "description": "List all commands"},
    {"command": "help", "description": "Show help"},
]


def _escape_html(text: str) -> str:
    """Escape text for Telegram's HTML parse mode — unescaped ``<``/``&`` are
    rejected (400) or rendered as entities by Telegram."""

    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def _to_html(text: str) -> str:
    """Escaped text with a light markdown subset rendered as HTML.

    Applied to agent content (bold, inline code, fenced blocks, links); the
    result stays safe because escaping runs first, so markdown markers in
    user data cannot smuggle raw tags through.
    """

    esc = _escape_html(text)
    esc = re.sub(
        r"```(?:\w+)?\n?(.*?)```",
        lambda m: f"<pre>{m.group(1)}</pre>",
        esc,
        flags=re.S,
    )
    esc = re.sub(r"`([^`]+)`", r"<code>\1</code>", esc)
    esc = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", esc)
    return re.sub(
        r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
        r'<a href="\2">\1</a>',
        esc,
    )


# Poll window / reconnect backoff are configurable via
# AVA_TELEGRAM_POLL_TIMEOUT_SECONDS / AVA_TELEGRAM_RECONNECT_*_DELAY_SECONDS
# (task #698 G8). Telegram caps the getUpdates long-poll window around 50s.
# Idle pause between polls when a batch completes with no work to do. A real
# getUpdates long-poll blocks up to the configured poll window, so this only
# matters when a poll returns instantly (mocked transports, degenerate
# servers) — without it the loop would busy-spin, starving the event loop and
# piling up garbage.
_POLL_IDLE_SLEEP = 0.05


def _split_text(text: str, limit: int = _MAX_MESSAGE_LEN) -> list[str]:
    """Split ``text`` into hard slices of at most ``limit`` chars."""
    return [text[i : i + limit] for i in range(0, len(text), limit)]


class _MarkupRejectedError(Exception):
    """Telegram refused the HTML entities (400) — send plain instead."""


class TelegramAdapter(IMAdapter):
    """Long-poll the owner's Telegram chat; forward owner text to the core."""

    channel = "telegram"

    def __init__(self, core: Any, *, client: httpx.AsyncClient | None = None) -> None:
        super().__init__(core)
        self._client = client  # injected in tests; else created lazily
        self._owns_client = client is None
        self._poll_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        # message_ids delivered in this process — a re-fetched update (the
        # offset only advances after a message is handled) must not deliver twice.
        self._handled_message_ids: set[str] = set()

    # -- config ----------------------------------------------------------

    @property
    def _token(self) -> str:
        return settings.telegram.telegram_bot_token

    @property
    def _owner_id(self) -> int:
        return settings.telegram.telegram_owner_id

    @property
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient()
            self._owns_client = True
        return self._client

    def _offset_path(self) -> Path:
        return Path(settings.general.ava_home) / "state" / "im_bridge" / "telegram_offset"

    # -- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        # Unconfigured = skip with a log, like the weixin/feishu adapters — the
        # daemon must stay up either way (a fresh install has no bot token, and
        # a raise here kills the session, failing `ava start`'s readiness gate).
        if not self._token or self._owner_id == 0:
            logger.info("telegram not configured (no bot token / owner id), skipping")
            return
        self._stop_event.clear()
        self._poll_task = asyncio.create_task(self._poll_loop(), name="telegram-poll")
        await self._install_command_menu()

    async def _install_command_menu(self) -> None:
        """The persistent menu beside the input field (setMyCommands). A
        failure is logged and tolerated — the bot still works via typed
        commands; the menu just won't show."""

        try:
            resp = await self._http.post(
                f"https://api.telegram.org/bot{self._token}/setMyCommands",
                json={"commands": _COMMAND_MENU},
                timeout=10.0,
            )
            if resp.status_code != 200:
                logger.warning("telegram setMyCommands failed: HTTP {}", resp.status_code)
        except httpx.HTTPError as exc:
            logger.warning("telegram setMyCommands failed: {}", type(exc).__name__)

    async def stop(self) -> None:
        self._stop_event.set()
        task, self._poll_task = self._poll_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
            self._owns_client = False

    # -- inbound ---------------------------------------------------------

    async def _poll_loop(self) -> None:
        delay = settings.telegram.telegram_reconnect_base_delay_seconds
        while not self._stop_event.is_set():
            try:
                updates = await self._get_updates()
                delay = settings.telegram.telegram_reconnect_base_delay_seconds
                for update in updates:
                    await self._handle_update(update)
                    # Ack only after the update was handled: a failure keeps the
                    # offset put and the update is re-fetched next poll — a
                    # message consumed during a rollout window is never dropped.
                    self._write_offset(update["update_id"] + 1)
                # Yield even on an instant empty batch so a fast/mocked poll
                # source cannot monopolize the event loop (see _POLL_IDLE_SLEEP).
                await asyncio.sleep(_POLL_IDLE_SLEEP)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # the loop must survive any failure
                logger.warning("telegram poll failed: {}", exc)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                delay = min(delay * 2, settings.telegram.telegram_reconnect_max_delay_seconds)

    async def _get_updates(self) -> list[dict[str, Any]]:
        offset = self._read_offset()
        try:
            resp = await self._http.get(
                f"https://api.telegram.org/bot{self._token}/getUpdates",
                params={
                    "offset": offset,
                    "timeout": settings.telegram.telegram_poll_timeout_seconds,
                    "limit": 100,
                    # Telegram wants this as a JSON-array string, not a repeated query param.
                    # callback_query is required or inline-keyboard taps are
                    # dropped by Telegram and never reach the poll loop.
                    "allowed_updates": json.dumps(["message", "callback_query"]),
                },
                timeout=settings.telegram.telegram_poll_timeout_seconds + 10.0,
            )
        except httpx.HTTPError as exc:
            # httpx error text can embed the request URL, which carries the token.
            raise RuntimeError(
                f"telegram getUpdates request failed: {type(exc).__name__}"
            ) from None
        if resp.status_code != 200:
            # Never raise_for_status(): HTTPStatusError embeds the request URL.
            # Response bodies are safe — they never contain the token.
            raise RuntimeError(
                f"telegram getUpdates failed: HTTP {resp.status_code} - {resp.text[:200]}"
            )
        return resp.json()["result"]

    async def _handle_update(self, update: dict[str, Any]) -> None:
        callback = update.get("callback_query")
        if callback is not None:
            await self._handle_callback(callback)
            return
        message = update.get("message")
        if message is None:
            return  # edited_message / channel_post / ... — not a plain inbound message
        if message.get("chat", {}).get("id") != self._owner_id:
            return  # stranger messaged the bot: consumed, never surfaced
        text = message.get("text") or message.get("caption")
        if not text:
            return  # media without a caption carries no text
        message_id = str(message["message_id"])
        if message_id in self._handled_message_ids:
            return  # re-fetched after a crash window — already delivered
        await self.core.handle_inbound(
            InboundMessage(
                channel=self.channel,
                chat_id=str(message["chat"]["id"]),
                text=text,
                message_id=message_id,
            )
        )
        self._handled_message_ids.add(message_id)
        if len(self._handled_message_ids) > 512:  # bound memory
            self._handled_message_ids = set(list(self._handled_message_ids)[-256:])

    async def _handle_callback(self, callback: dict[str, Any]) -> None:
        """An inline-keyboard tap: run the button's command like a typed
        message, then acknowledge the tap so Telegram clears the spinner."""

        message: dict[str, Any] = callback.get("message") or {}
        chat_id = str(message.get("chat", {}).get("id") or "")
        data = str(callback.get("data") or "").strip()
        if not chat_id or not data:
            return
        if message.get("chat", {}).get("id") != self._owner_id:
            return  # stranger tapped a button: consumed, never surfaced
        await self.core.handle_inbound(
            InboundMessage(
                channel=self.channel,
                chat_id=chat_id,
                text=data,
                message_id=str(message.get("message_id") or ""),
            )
        )
        await self._answer_callback(str(callback.get("id") or ""))

    async def _answer_callback(self, callback_id: str) -> None:
        if not callback_id:
            return
        try:
            resp = await self._http.post(
                f"https://api.telegram.org/bot{self._token}/answerCallbackQuery",
                json={"callback_query_id": callback_id},
                timeout=10.0,
            )
            if resp.status_code != 200:
                logger.warning("telegram answerCallbackQuery failed: HTTP {}", resp.status_code)
        except httpx.HTTPError as exc:
            logger.warning("telegram answerCallbackQuery failed: {}", type(exc).__name__)

    # -- outbound --------------------------------------------------------

    can_buttons = True  # inline keyboards (the tap-to-switch /list card)
    can_type = True  # sendChatAction typing — the native processing indicator

    async def typing(self, chat_id: str) -> None:
        """Show Telegram's native \"typing…\" indicator (the bouncing dots)
        — the real processing signal, not a placeholder message. Telegram
        clears it after ~5s, so the core refreshes it until the reply lands."""

        try:
            resp = await self._http.post(
                f"https://api.telegram.org/bot{self._token}/sendChatAction",
                json={"chat_id": chat_id, "action": "typing"},
                timeout=10.0,
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"telegram typing failed: {type(exc).__name__}") from None
        if resp.status_code != 200:
            raise RuntimeError(
                f"telegram typing failed: HTTP {resp.status_code} - {resp.text[:200]}"
            )

    async def send(
        self,
        chat_id: str,
        text: str,
        *,
        buttons: list[tuple[str, str]] | None = None,
        markdown: bool = False,
    ) -> None:
        """Send text as HTML (buttons as an inline keyboard); fall back to
        plain text if Telegram rejects the markup."""

        for chunk in _split_text(text):
            rendered = _to_html(chunk) if markdown else _escape_html(chunk)
            try:
                await self._send_message(chat_id, rendered, buttons=buttons, html=True)
            except _MarkupRejectedError:
                # Telegram 400'd the entities — resend the raw chunk as plain
                # text so the message still lands (formatting lost, content
                # kept; no escaped entities shown literally).
                await self._send_message(chat_id, chunk, buttons=buttons, html=False)

    async def send_to_owner(
        self,
        text: str,
        *,
        markdown: bool = False,
        buttons: list[tuple[str, str]] | None = None,
    ) -> None:
        """Send an outbound notification to the owner's private chat."""

        owner = self._owner_id
        if not owner:
            raise RuntimeError("telegram owner chat not configured (AVA_TELEGRAM_OWNER_ID)")
        await self.send(str(owner), text, markdown=markdown, buttons=buttons)

    async def _send_message(
        self,
        chat_id: str,
        text: str,
        *,
        buttons: list[tuple[str, str]] | None,
        html: bool,
    ) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if html:
            payload["parse_mode"] = "HTML"
        if buttons:
            payload["reply_markup"] = {
                "inline_keyboard": [
                    [{"text": label, "callback_data": command}] for label, command in buttons
                ]
            }
        try:
            resp = await self._http.post(
                f"https://api.telegram.org/bot{self._token}/sendMessage",
                json=payload,
                timeout=10.0,
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"telegram send failed: {type(exc).__name__}") from None
        if resp.status_code == 400 and html:
            raise _MarkupRejectedError  # bad entities — caller retries without parse_mode
        if resp.status_code != 200:
            raise RuntimeError(f"telegram send failed: HTTP {resp.status_code} - {resp.text[:200]}")

    # -- offset persistence ----------------------------------------------

    def _read_offset(self) -> int:
        path = self._offset_path()
        if not path.exists():
            return 0
        raw = path.read_text().strip()
        return int(raw) if raw else 0

    def _write_offset(self, offset: int) -> None:
        path = self._offset_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(str(offset))
        tmp.replace(path)  # atomic on POSIX: no reader sees a half-written offset


ADAPTER_CLASS = TelegramAdapter
