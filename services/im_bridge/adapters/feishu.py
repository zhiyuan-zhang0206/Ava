"""Feishu adapter for IM Bridge — enterprise self-built app + bot.

Connects over the lark-oapi WebSocket long connection (``im.message.receive_v1``
event), so no public IP is needed. Only p2p (private-chat) text messages are
bridged; group chats and media messages are ignored. Replies go through the
REST ``im/v1/messages`` create API keyed by the sender's ``open_id`` (the
contract's feishu session id).

Credentials are read from ``settings.feishu`` when that config domain exists,
else from env vars (``AVA_FEISHU_APP_ID`` / ``FEISHU_APP_ID`` and
``AVA_FEISHU_APP_SECRET`` / ``FEISHU_APP_SECRET``). Missing credentials make
``start()`` log and no-op — the daemon keeps running without the feishu link.

Platform side (operator action, not code): in the Feishu open platform enable
the app's long-connection event subscription and add the "message received"
(``im.message.receive_v1``) event; the bot must be available in the p2p chat.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import threading
from collections import deque
from typing import Any

from services.im_bridge.core import IMAdapter, InboundMessage
from shared.log import logger

# Feishu caps a text message around 30KB of characters; segment conservatively.
MAX_SEGMENT_CHARS = 8000
# A message that repeatedly crashes inbound handling is skipped after this
# many consecutive failures (see the poller's poison-message handling).
POISON_MAX_RETRIES = 3


def _backoff_delay(interval: float, failures: int) -> float:
    """Poll delay after `failures` consecutive all-chats-failed rounds.

    Doubles the interval per failure (capped at 32x); the absolute cap never
    drops below the configured interval, so a >300s interval keeps its own
    cadence instead of being clamped to 300s.
    """
    return min(interval * (2 ** min(failures, 5)), max(300.0, interval))


class FeishuAdapter(IMAdapter):
    """Bridge p2p text messages between Feishu and the IM Bridge core.

    The lark-oapi WebSocket client is synchronous and blocks forever inside
    ``Client.start()``; it also binds a module-level event loop on its FIRST
    import (``lark_oapi/ws/client.py`` calls ``asyncio.get_event_loop()`` at
    module scope). Every lark import therefore happens lazily inside the
    dedicated ws thread, so the SDK's loop is a fresh thread-local loop and
    ``start()``'s ``run_until_complete`` never touches the daemon's own loop.
    The SDK reconnects with backoff automatically (``auto_reconnect`` default).
    Outbound uses a separate REST ``lark.Client`` run via ``asyncio.to_thread``.
    """

    channel = "feishu"

    def __init__(self, core: Any) -> None:
        super().__init__(core)
        self._app_id = ""
        self._app_secret = ""
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._ws_loop: asyncio.AbstractEventLoop | None = None
        self._ws_thread: threading.Thread | None = None
        self._ws_client: Any = None
        self._rest_client: Any = None
        # Initialized here (not only in _normalize) so send_to_owner before the
        # first inbound p2p — e.g. right after a daemon restart — fails with the
        # clear "no known user chat" error instead of an AttributeError.
        self._last_open_id = ""
        # Polling fallback state: the platform does not deliver
        # im.message.receive_v1 for this app (2026-09-01 diagnosis), so p2p
        # messages are picked up by polling ListMessage instead. Chats are
        # discovered from outbound send responses and/or the bootstrap config.
        self._poll_task: asyncio.Task[Any] | None = None
        self._sent_chat_ids: dict[str, str] = {}  # open_id -> p2p chat id (from sends)
        self._poll_chats: set[str] = set()
        self._poll_cursor: dict[str, str] = {}  # chat_id -> last delivered message_id
        # Seeded chats are tracked separately from the cursor value: a
        # successful round with an empty window still marks the chat seeded,
        # so the first message that arrives afterwards is delivered instead
        # of being mistaken for history (a fresh chat's first round is empty).
        self._poll_seeded: set[str] = set()
        self._poll_failures = 0  # consecutive all-chats-failed rounds (backoff)
        self._poison_retries: dict[str, int] = {}  # "chat:msg" -> inbound failures
        self._seen_messages: deque[str] = deque(maxlen=500)

    # -- credentials ---------------------------------------------------------

    @staticmethod
    def _credential(field: str, *env_names: str) -> str:
        """Read a credential from ``settings.feishu`` — the repo's only
        sanctioned env surface (lint os.environ forbids direct reads).
        ``env_names`` is accepted for call-site clarity but unused."""
        del env_names
        try:
            from shared.config import settings

            domain = getattr(settings, "feishu", None)
            if domain is not None:
                value: Any = getattr(domain, field, "")
                if value:
                    return (
                        value.get_secret_value()
                        if hasattr(value, "get_secret_value")
                        else str(value)
                    )
        except Exception:
            # shared.config must never break the adapter (settings-lite verbs,
            # bare checkouts, a gateway fetch failure).
            logger.debug("FeishuAdapter: settings.feishu probe failed")
        return ""

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Connect the long-connection client; no-op (with a log) when the
        credentials are missing so the daemon stays up either way."""
        self._app_id = self._credential("feishu_app_id")
        self._app_secret = self._credential("feishu_app_secret")
        if not self._app_id or not self._app_secret:
            logger.warning(
                "FeishuAdapter: FEISHU_APP_ID / FEISHU_APP_SECRET not configured; "
                "feishu link disabled (set the credentials to enable)"
            )
            return
        self._main_loop = asyncio.get_running_loop()
        self._ws_thread = threading.Thread(target=self._run_ws, name="feishu-ws", daemon=True)
        self._ws_thread.start()
        logger.info("FeishuAdapter: ws thread started")
        self._start_poller()

    def _run_ws(self) -> None:
        """Connect the long-connection client; blocks for the process lifetime.

        Runs in a dedicated daemon thread (see the class docstring for why every
        lark import lives here). A failed connect is logged and the SDK retries
        with backoff; a failure here must never take the daemon down.
        """
        try:
            self._ws_client = self._build_ws_client()
            import lark_oapi.ws.client as _ws_module  # pyright: ignore[reportUnknownVariableType]

            self._ws_loop = _ws_module.loop  # pyright: ignore[reportUnknownMemberType]
            self._ws_client.start()  # blocks; SDK reconnects internally
        except Exception as exc:
            logger.error("FeishuAdapter: ws connection failed: {}", exc)

    async def stop(self) -> None:
        """Close the ws connection (the SDK has no public stop; its private
        ``_disconnect`` is scheduled on the SDK's own loop). The ws thread is a
        daemon and lingers harmlessly until process exit."""
        poll_task = self._poll_task
        self._poll_task = None
        if poll_task is not None:
            poll_task.cancel()
        ws_client = self._ws_client
        ws_loop = self._ws_loop
        self._ws_client = None
        self._ws_loop = None
        if ws_client is None or ws_loop is None:
            return
        try:
            future = asyncio.run_coroutine_threadsafe(
                # SLF001: the SDK has no public stop; _disconnect is the seam.
                ws_client._disconnect(),
                ws_loop,
            )
        except Exception as exc:
            logger.warning("FeishuAdapter: scheduling ws disconnect failed: {}", exc)
            return
        future.add_done_callback(_log_future_error)

    # -- lark construction (lazy; the first import binds the SDK's loop) -----

    def _build_ws_client(self) -> Any:
        """Build the long-connection client + event handler (ws thread only)."""
        import lark_oapi as lark  # pyright: ignore[reportUnknownVariableType]

        handler: Any = lark.EventDispatcherHandler.builder(  # pyright: ignore[reportUnknownMemberType]
            "", ""
        )
        handler = handler.register_p2_im_message_receive_v1(self._on_im_message)
        try:
            # Card button callbacks ride the long connection too (official SDK
            # support); the try keeps older lark-oapi versions working where the
            # v2 card event is not registered by name.
            handler = handler.register_p2_card_action_trigger(self._on_card_action)
        except (AttributeError, TypeError):
            logger.warning("FeishuAdapter: card.action.trigger not available in lark-oapi")
        handler = handler.build()
        client: Any = lark.ws.Client(  # pyright: ignore[reportUnknownMemberType]
            self._app_id, self._app_secret, event_handler=handler
        )
        return client

    def _build_rest_client(self) -> Any:
        """Build the REST client used for sending (worker thread)."""
        import lark_oapi as lark  # pyright: ignore[reportUnknownVariableType]

        builder: Any = lark.Client.builder()  # pyright: ignore[reportUnknownMemberType]
        # Explicit timeout (G6, task #698): the lark SDK's own default is 30s
        # but that is an SDK-version property, not a contract — pin it so a hung
        # Feishu REST line cannot park an IM outbound longer than configured.
        # Same fail-open guard as _credential: settings must never break the
        # adapter in a settings-lite context.
        try:
            from shared.config import settings

            timeout = settings.feishu.feishu_rest_timeout_seconds
        except Exception:
            timeout = 30.0
        return builder.app_id(self._app_id).app_secret(self._app_secret).timeout(timeout).build()

    # -- inbound -------------------------------------------------------------

    def _on_im_message(self, data: Any) -> None:
        """SDK event-handler callback (ws thread) — dispatch on the main loop."""
        main_loop = self._main_loop
        if main_loop is None or main_loop.is_closed():
            return
        future = asyncio.run_coroutine_threadsafe(self._handle_event(data), main_loop)
        future.add_done_callback(_log_future_error)

    def _on_card_action(self, data: Any) -> None:
        """Card button callback (ws thread) — the button's value.key is the
        command; feed it through core as a text message from the operator."""
        main_loop = self._main_loop
        if main_loop is None or main_loop.is_closed():
            return
        future = asyncio.run_coroutine_threadsafe(self._handle_card_action(data), main_loop)
        future.add_done_callback(_log_future_error)

    async def _handle_card_action(self, data: Any) -> None:
        try:
            action = getattr(data, "event", None)
            if action is None:
                return
            operator = getattr(action, "operator", None)
            open_id = getattr(operator, "open_id", "") if operator is not None else ""
            if not open_id:
                return
            card_action: Any = getattr(action, "action", None)
            if card_action is None:
                return
            card_value: dict[str, object] = getattr(card_action, "value", None) or {}
            key = str(card_value.get("key", ""))
            if not key:
                return
            # Button taps are the user pressing a command — same routing as
            # typed text (commands, notice callbacks, spawn menus).
            self._last_open_id = open_id
            await self.core.handle_inbound(
                InboundMessage(
                    channel=self.channel,
                    chat_id=open_id,
                    text=key,
                )
            )
        except Exception as exc:
            logger.error("FeishuAdapter: card action failed: {}", exc)

    async def _handle_event(self, data: Any) -> None:
        try:
            message = self._normalize(data)
        except Exception as exc:
            # A bad payload must not break the event loop.
            logger.warning("FeishuAdapter: dropped malformed event: {}", exc)
            return
        if message is None:
            return  # group chat / non-text / empty — intentionally ignored
        try:
            await self.core.handle_inbound(message)
        except Exception as exc:
            # Core errors are core's to handle; never crash the event loop here.
            logger.error("FeishuAdapter: core.handle_inbound failed: {}", exc)

    def _normalize(self, data: Any) -> InboundMessage | None:
        """Map a ``P2ImMessageReceiveV1`` (or duck-typed stand-in) to an
        InboundMessage; return None for events we do not bridge."""
        event = getattr(data, "event", None)
        message = getattr(event, "message", None)
        sender = getattr(event, "sender", None)
        if message is None or sender is None:
            return None
        if getattr(message, "chat_type", "") != "p2p":
            return None  # group chats are not bridged
        if getattr(message, "message_type", "") != "text":
            return None  # only plain text is bridged
        if getattr(sender, "sender_type", "") != "user":
            return None  # the bot's own messages must not echo back into core
        content = getattr(message, "content", "") or ""
        try:
            payload: dict[str, Any] = json.loads(content)
            text = str(payload.get("text", "")).strip()
        except (TypeError, ValueError):
            logger.warning("FeishuAdapter: unparseable text content: {!r:.120}", content)
            return None
        if not text:
            return None
        sender_id = getattr(sender, "sender_id", None)
        open_id = getattr(sender_id, "open_id", "") if sender_id is not None else ""
        if not open_id:
            return None
        message_id = getattr(message, "message_id", None)
        # The polling fallback may have already fed this message (or vice
        # versa): a shared seen-set makes the two paths idempotent.
        if message_id in self._seen_messages:
            return None
        if message_id:
            self._seen_messages.append(message_id)
        self._last_open_id = open_id  # remember the p2p peer for notify_user
        return InboundMessage(
            channel=self.channel,
            chat_id=open_id,  # contract: the feishu session IS the user's open_id
            text=text,
            message_id=message_id,
        )

    # -- outbound ------------------------------------------------------------

    async def _register_sent_chat(self, open_id: str) -> None:
        """Register the p2p chat resolved from the last outbound send.

        ListMessage needs the chat id, which the WS event never provided for
        this app; the create-message response carries it, so every outbound
        send teaches the poller one more chat. Fails softly — polling is a
        fallback, never a reason to break a send.
        """
        try:
            chat_id = self._sent_chat_ids.get(open_id)
            if chat_id:
                self._poll_chats.add(chat_id)
                logger.info(
                    "FeishuAdapter: poller registered chat %s for open_id %s", chat_id, open_id
                )
        except Exception as exc:
            logger.debug("FeishuAdapter: chat registration failed: {}", exc)

    # -- polling fallback ------------------------------------------------------

    def _start_poller(self) -> None:
        """Start the ListMessage poll loop (main loop task)."""
        if self._poll_task is not None and not self._poll_task.done():
            return
        try:
            from shared.config import settings

            interval = settings.feishu.feishu_poll_interval_seconds
            bootstrap = (settings.feishu.feishu_poll_chat_id or "").strip()
        except Exception:
            interval = 1.0
            bootstrap = ""
        if interval <= 0:
            logger.info("FeishuAdapter: polling disabled (AVA_FEISHU_POLL_INTERVAL_SECONDS=0)")
            return
        if bootstrap:
            self._poll_chats.add(bootstrap)
        if not self._poll_chats:
            # No chat known yet: outbound sends add chats as they resolve.
            logger.info(
                "FeishuAdapter: poller idle — no chat id known yet "
                "(set AVA_FEISHU_POLL_CHAT_ID or wait for an outbound send)"
            )
        self._poll_task = asyncio.create_task(self._poll_loop(interval))
        logger.info(
            "FeishuAdapter: poller started interval=%.1fs chats=%s",
            interval,
            sorted(self._poll_chats),
        )

    async def _poll_loop(self, interval: float) -> None:
        """Poll every known p2p chat forever; one bad round never kills it.

        Backoff is all-or-nothing: a round where EVERY chat failed backs off
        exponentially (a hard-down API is not hammered), while a round where
        only some chats failed keeps the normal cadence — one chat's
        persistent failure (e.g. the user deleted the bot) must never slow
        the healthy chats. Any fully-healthy round resets the backoff.
        """
        while True:
            failed = 0
            total = len(self._poll_chats)
            try:
                for chat_id in list(self._poll_chats):
                    try:
                        if not await self._poll_once(chat_id):
                            failed += 1
                    except Exception as exc:
                        failed += 1
                        logger.error("FeishuAdapter: poll failed chat=%s: {}", chat_id, exc)
            except Exception:
                failed = total
                logger.exception("FeishuAdapter: poll round failed")
            delay = self._round_delay(failed, total, interval)
            await asyncio.sleep(delay)

    def _round_delay(self, failed: int, total: int, interval: float) -> float:
        """Poll delay after one round, mutating the all-chats-failed counter.

        All-or-nothing backoff: a round where every chat failed backs off
        exponentially; any round with at least one healthy chat keeps the
        normal cadence and resets the counter.
        """
        if failed == 0 or failed < total:
            self._poll_failures = 0
            return interval
        self._poll_failures += 1
        return _backoff_delay(interval, self._poll_failures)

    async def _poll_once(self, chat_id: str) -> bool:
        """List the chat's newest messages; feed unseen user texts to core.

        The first successful round only seeds the cursor (never replays
        history on a daemon restart); later rounds process messages newer
        than the cursor, oldest first, deduped by message id — the WS path
        may deliver the same message concurrently. Returns False when the
        list call failed (drives the poll loop's backoff); the cursor only
        advances past messages that were delivered or are permanently
        undeliverable, so a failed inbound is retried next round.
        """
        if self._rest_client is None:
            self._rest_client = await asyncio.to_thread(self._build_rest_client)
        from lark_oapi.api.im.v1 import ListMessageRequest

        request = (
            ListMessageRequest.builder()
            .container_id_type("chat")
            .container_id(chat_id)
            .page_size(20)
            .sort_type("ByCreateTimeDesc")
            .build()
        )
        response = await asyncio.to_thread(self._rest_client.im.v1.message.list, request)
        if response.code != 0:
            logger.warning(
                "FeishuAdapter: poll list failed chat=%s code=%s msg=%s",
                chat_id,
                response.code,
                getattr(response, "msg", ""),
            )
            return False
        items: list[Any] = list((response.data.items or []) if response.data is not None else [])
        items.reverse()  # ascending by create time
        if chat_id not in self._poll_seeded:
            # Seed round: remember the newest id without processing anything.
            # The chat is marked seeded even when the window is empty, so the
            # first real message afterwards is delivered rather than swallowed
            # as "history". A failed round never marks seeded, preserving the
            # no-replay guarantee across daemon restarts.
            self._poll_seeded.add(chat_id)
            # Anchor on the newest item that carries an id: an id-less newest
            # item (defensive — the API guarantees ids) must not leave the
            # chat without a cursor, which would replay the whole window.
            seed_item = next((item for item in reversed(items) if item.message_id), None)
            if seed_item is not None:
                self._poll_cursor[chat_id] = seed_item.message_id
            return True
        cursor = self._poll_cursor.get(chat_id)
        # Process everything after the cursor (ascending order); when the
        # cursor rotated out of the window, fall back to the seen-id dedup.
        pending = items
        if cursor is not None:
            for idx, item in enumerate(items):
                if item.message_id == cursor:
                    pending = items[idx + 1 :]
                    break
        last = await self._poll_deliver(pending, chat_id)
        if last:
            self._poll_cursor[chat_id] = last
        return True

    async def _poll_deliver(self, pending: list[Any], chat_id: str) -> str | None:
        """Feed unseen user texts to core; return the newest message id the
        cursor may advance to (delivered, skipped, or permanently
        undeliverable), or None when a delivery failed and the round must
        not advance past it.

        A message that keeps crashing inbound handling is skipped after
        POISON_MAX_RETRIES consecutive failures with a loud log, so it cannot
        wedge the chat forever (core swallows nearly everything, so this is a
        last resort, not a normal path).
        """
        last: str | None = None
        for item in pending:
            if not item.message_id:
                # Cannot dedup or cursor on an id-less item; never deliver.
                continue
            key = f"{chat_id}:{item.message_id}"
            if item.message_id in self._seen_messages:
                last = item.message_id
                continue
            message = self._normalize_poll_item(item)
            if message is None:
                # Permanently undeliverable (bot send, non-text, malformed):
                # safe to pass, otherwise the same item is re-listed forever.
                last = item.message_id
                continue
            try:
                await self.core.handle_inbound(message)
            except Exception:
                retries = self._poison_retries.get(key, 0) + 1
                self._poison_retries[key] = retries
                if retries < POISON_MAX_RETRIES:
                    logger.exception("FeishuAdapter: poll inbound failed chat=%s", chat_id)
                    break  # do not advance past the failure; retry next round
                self._poison_retries.pop(key, None)
                logger.error(
                    "FeishuAdapter: poll inbound failed %d times chat=%s msg=%s; skipping message",
                    retries,
                    chat_id,
                    item.message_id,
                )
                self._seen_messages.append(item.message_id)
                last = item.message_id
                continue
            self._seen_messages.append(item.message_id)
            self._poison_retries.pop(key, None)
            last = item.message_id
        return last

    def _normalize_poll_item(self, item: Any) -> InboundMessage | None:
        """Map one listed message to an InboundMessage (same contract as the
        WS event path: p2p text from a user, never our own sends)."""
        if not getattr(item, "message_id", ""):
            return None
        if getattr(item, "chat_type", "") != "p2p":
            return None
        if getattr(item, "msg_type", "") != "text":
            return None
        sender = getattr(item, "sender", None)
        if sender is None or getattr(sender, "sender_type", "") != "user":
            return None
        content = ""
        try:
            content = (
                json.loads(getattr(getattr(item, "body", None), "content", "") or "{}")
                .get("text", "")
                .strip()
            )
        except (TypeError, ValueError):
            return None
        if not content:
            return None
        sender_id = getattr(sender, "sender_id", None)
        open_id = getattr(sender_id, "open_id", "") if sender_id is not None else ""
        if not open_id:
            return None
        self._last_open_id = open_id
        return InboundMessage(
            channel=self.channel,
            chat_id=open_id,
            text=content,
            message_id=getattr(item, "message_id", None),
        )

    async def send(
        self,
        chat_id: str,
        text: str,
        *,
        buttons: list[tuple[str, str]] | None = None,
        markdown: bool = False,
    ) -> None:
        """Send a message to a user (open_id).

        With ``buttons`` this is an interactive card (the button callback
        value is the command, routed through core.handle_inbound exactly like
        a typed command); without, plain text, segmenting at
        MAX_SEGMENT_CHARS (a card must not be segmented — buttons belong to
        the whole message). ``markdown`` is accepted for the shared contract
        but not rendered (text messages are plain)."""

        del markdown  # platform contract: accepted, not rendered
        if not self._app_id or not self._app_secret:
            raise RuntimeError(
                "feishu send failed: adapter not configured "
                "(missing FEISHU_APP_ID / FEISHU_APP_SECRET)"
            )
        if self._ws_thread is None or not self._ws_thread.is_alive():
            raise RuntimeError("feishu send failed: adapter not started")
        if self._rest_client is None:
            self._rest_client = await asyncio.to_thread(self._build_rest_client)
        if buttons:
            await asyncio.to_thread(self._send_card, self._rest_client, chat_id, text, buttons)
        else:
            for segment in _segment(text, MAX_SEGMENT_CHARS):
                await asyncio.to_thread(self._send_one, self._rest_client, chat_id, segment)
        # The send response carries the p2p chat id — register it for polling
        # so the user's next message is picked up without the WS event path.
        await self._register_sent_chat(chat_id)

    async def send_to_owner(self, text: str, *, markdown: bool = False) -> None:
        """Send an outbound notification to the last p2p sender (the user).

        Feishu has no configured owner id — the only chat we know is the
        p2p sender of an inbound message; before the first message there is
        nowhere to send and the fan-out skips this channel.
        """

        del markdown  # platform contract: accepted, not rendered
        if not self._last_open_id:
            raise RuntimeError("feishu: no known user chat yet")
        await self.send(self._last_open_id, text)

    def _send_card(
        self, client: Any, chat_id: str, text: str, buttons: list[tuple[str, str]]
    ) -> None:
        """Send an interactive card whose buttons carry the callback values.

        The card's ``value.key`` is the same command string the button label
        stands for (e.g. ``/list`` or ``notice:read:7:42``); the card callback
        handler feeds it back into core.handle_inbound as a text message, so
        every existing command / notice callback works on Feishu untouched.
        """

        from lark_oapi.api.im.v1 import (  # pyright: ignore[reportUnknownVariableType]
            CreateMessageRequest,
            CreateMessageRequestBody,
        )

        actions = [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": label},
                "value": {"key": callback},
            }
            for label, callback in buttons
        ]
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "Ava"},
                "template": "blue",
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": text}},
                {"tag": "action", "actions": actions},
            ],
        }
        request: Any = (
            CreateMessageRequest.builder()  # pyright: ignore[reportUnknownMemberType]
            .receive_id_type("open_id")
            .request_body(
                CreateMessageRequestBody.builder()  # pyright: ignore[reportUnknownMemberType]
                .receive_id(chat_id)
                .msg_type("interactive")
                .content(json.dumps(card))
                .build()
            )
            .build()
        )
        response = client.im.v1.message.create(request)
        if not response.success():
            raise RuntimeError(f"feishu card send failed: code={response.code} msg={response.msg}")
        data = response.data
        chat_id_resolved = getattr(data, "chat_id", "") if data is not None else ""
        if chat_id_resolved:
            self._sent_chat_ids[chat_id] = chat_id_resolved

    def _send_one(self, client: Any, chat_id: str, text: str) -> str:
        """Send one text segment; returns the p2p chat id from the response
        ("" when the response does not carry one)."""
        from lark_oapi.api.im.v1 import (  # pyright: ignore[reportUnknownVariableType]
            CreateMessageRequest,
            CreateMessageRequestBody,
        )

        request: Any = (
            CreateMessageRequest.builder()  # pyright: ignore[reportUnknownMemberType]
            .receive_id_type("open_id")
            .request_body(
                CreateMessageRequestBody.builder()  # pyright: ignore[reportUnknownMemberType]
                .receive_id(chat_id)
                .msg_type("text")
                .content(json.dumps({"text": text}))
                .build()
            )
            .build()
        )
        response = client.im.v1.message.create(request)
        if not response.success():
            # Sanitized on purpose: the SDK response may embed request internals
            # but never credentials; keep it that way in the raised error.
            raise RuntimeError(f"feishu send failed: code={response.code} msg={response.msg}")
        data = response.data
        chat_id_resolved = getattr(data, "chat_id", "") if data is not None else ""
        if chat_id_resolved:
            self._sent_chat_ids[chat_id] = chat_id_resolved
        return chat_id_resolved


def _segment(text: str, limit: int) -> list[str]:
    """Split text into ``<=limit``-char chunks (empty text → no chunks)."""
    return [text[i : i + limit] for i in range(0, len(text), limit)]


def _log_future_error(future: concurrent.futures.Future[Any]) -> None:
    """Surface an exception a scheduled coroutine raised (else swallow)."""
    try:
        future.result()
    except Exception as exc:
        logger.warning("FeishuAdapter: background task failed: {}", exc)


ADAPTER_CLASS = FeishuAdapter
