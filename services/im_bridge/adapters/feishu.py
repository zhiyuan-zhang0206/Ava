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
from typing import Any

from services.im_bridge.core import IMAdapter, InboundMessage
from shared.log import logger

# Feishu caps a text message around 30KB of characters; segment conservatively.
MAX_SEGMENT_CHARS = 8000


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
        self._last_open_id = open_id  # remember the p2p peer for notify_user
        return InboundMessage(
            channel=self.channel,
            chat_id=open_id,  # contract: the feishu session IS the user's open_id
            text=text,
            message_id=getattr(message, "message_id", None),
        )

    # -- outbound ------------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        text: str,
        *,
        buttons: list[tuple[str, str]] | None = None,
        markdown: bool = False,
    ) -> None:
        """Send text to a user (open_id), segmenting at MAX_SEGMENT_CHARS.

        ``buttons`` / ``markdown`` are accepted for the shared adapter contract
        but not rendered on Feishu (plain text only)."""

        del buttons, markdown  # platform contract: accepted, not rendered
        if not self._app_id or not self._app_secret:
            raise RuntimeError(
                "feishu send failed: adapter not configured "
                "(missing FEISHU_APP_ID / FEISHU_APP_SECRET)"
            )
        if self._ws_thread is None or not self._ws_thread.is_alive():
            raise RuntimeError("feishu send failed: adapter not started")
        if self._rest_client is None:
            self._rest_client = await asyncio.to_thread(self._build_rest_client)
        for segment in _segment(text, MAX_SEGMENT_CHARS):
            await asyncio.to_thread(self._send_one, self._rest_client, chat_id, segment)

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

    def _send_one(self, client: Any, chat_id: str, text: str) -> None:
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
