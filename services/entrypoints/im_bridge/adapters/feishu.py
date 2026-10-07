"""Feishu enterprise bot: private text over lark-oapi WS events and REST sends.

Credentials come from FeishuCredentialsConfig; missing credentials skip start.
The operator enables im.message.receive_v1 long-connection subscriptions and
makes the bot available in the p2p chat. Polling recovers missed WS events.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import threading
from collections import deque
from typing import Any

from base.log import logger
from services.entrypoints.im_bridge.adapters import feishu_poll_cursor as cursors
from services.entrypoints.im_bridge.adapters.feishu_ws_proxy import allow_env_proxy_for_ws
from services.entrypoints.im_bridge.config import FeishuCredentialsConfig
from services.entrypoints.im_bridge.outbound_types import (
    OutboundAdapterKind,
    OutboundChunk,
    PreparedOutboundSend,
)
from services.entrypoints.im_bridge.state import _load_switch_state
from services.entrypoints.im_bridge.types import IMAdapter, InboundMessage

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
    """Bridge enterprise bot p2p text through WS events and polling fallback.

    The lark WS SDK binds an event loop at its first import and blocks in
    Client.start(). Import it lazily in the dedicated WS thread so its
    run_until_complete never touches the daemon loop; SDK reconnect stays
    automatic. Outbound uses a separate REST client via asyncio.to_thread.
    """

    channel = "feishu"

    def __init__(self, core: Any, config: FeishuCredentialsConfig) -> None:
        super().__init__(core)
        self._config = config
        self._replay_window_s = config.delivery_watchdog_stale_claimed_threshold_seconds
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
        # Cursor position's create time (ms); saved with the cursor so a restart
        # resumes there. `_poll_replay`: saved chats awaiting their first round.
        self._poll_cursor_ms: dict[str, int] = {}
        self._poll_replay: set[str] = set()
        # Seeded chats are tracked separately from the cursor value: a
        # successful round with an empty window still marks the chat seeded,
        # so the first message that arrives afterwards is delivered instead
        # of being mistaken for history (a fresh chat's first round is empty).
        self._poll_seeded: set[str] = set()
        self._poll_failures = 0  # consecutive all-chats-failed rounds (backoff)
        self._poison_retries: dict[str, int] = {}  # "chat:msg" -> inbound failures
        self._seen_messages: deque[str] = deque(maxlen=500)

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Connect the long-connection client; no-op (with a log) when the
        credentials are missing so the daemon stays up either way."""
        self._app_id = self._config.feishu_app_id
        self._app_secret = self._config.feishu_app_secret
        if not self._app_id or not self._app_secret:
            logger.warning(
                "FeishuAdapter: FEISHU_APP_ID / FEISHU_APP_SECRET not configured; "
                "feishu link disabled (set the credentials to enable)"
            )
            return
        # Seed the memory-only owner open id lost by a restart (task #4930).
        await self._seed_owner_from_switch_state()
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

    async def _seed_owner_from_switch_state(self) -> None:
        """Seed the owner open id lost by a restart from the persisted switch state.

        Exactly one ``feishu:<open_id>`` entry restores it, never overwriting an
        owner already known; anything else keeps it empty and emits the failure event.
        """
        if self._last_open_id:
            return
        prefix = f"{self.channel}:"
        try:
            keys = [k for k in _load_switch_state() if k.startswith(prefix) and k != prefix]
        except Exception as exc:
            logger.warning("FeishuAdapter: switch state unreadable: {!r}", exc)
            keys = []
        if len(keys) == 1:
            self._last_open_id = keys[0][len(prefix) :]
            logger.info("FeishuAdapter: seeded owner open id: {}", self._last_open_id)
            return
        logger.error(
            "FeishuAdapter: owner seed failed; feishu notifications are blind",
            event="im_feishu_owner_seed_failed",
            reason="ambiguous" if len(keys) > 1 else "no_source",
            chats=len(keys),
        )

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
        # Let the handshake use the machine's proxy configuration (issue #2089):
        # the SDK pins proxy=None on websockets>=15, which makes the long
        # connection direct-only and therefore unreachable behind a proxy.
        allow_env_proxy_for_ws()
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
        timeout = self._config.feishu_rest_timeout_seconds
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
            idempotency_key=cursors.idempotency_key(message_id),
        )

    # -- outbound ------------------------------------------------------------

    async def _register_sent_chat(self, open_id: str) -> None:
        """Register the p2p chat resolved from the last outbound send.

        ListMessage needs the chat id, which the WS event never provided for
        this app; the create-message response carries it, so every outbound
        send teaches the poller one more chat.
        Restore the owner open id if the restart seed round found no user message.
        """
        chat_id = self._sent_chat_ids.get(open_id)
        if chat_id:
            self._poll_chats.add(chat_id)
            logger.info("FeishuAdapter: poller registered chat {} for open_id {}", chat_id, open_id)
        if not self._last_open_id:
            self._last_open_id = open_id

    # -- polling fallback ------------------------------------------------------

    def _start_poller(self) -> None:
        """Start the ListMessage poll loop (main loop task)."""
        if self._poll_task is not None and not self._poll_task.done():
            return
        interval = self._config.feishu_poll_interval_seconds
        bootstrap = self._config.feishu_poll_chat_id.strip()
        if interval <= 0:
            logger.info("FeishuAdapter: polling disabled (AVA_FEISHU_POLL_INTERVAL_SECONDS=0)")
            return
        if bootstrap:
            self._poll_chats.add(bootstrap)
        self._poll_task = asyncio.create_task(self._poll_loop(interval))
        logger.info(
            "FeishuAdapter: poller started interval={:.1f}s chats={}",
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
        loaded = False
        # quiesce-exempt: polls the Feishu API; a cursor is written only when an update arrives, and forwarding goes through the gateway, which refuses business requests in the window
        while True:
            if not loaded:
                # before any round: an unrestored chat would seed, not replay
                try:
                    await self._restore_cursors()
                    loaded = True
                except Exception:
                    logger.exception("FeishuAdapter: restoring poll cursors failed")
                    await asyncio.sleep(interval)
                    continue
            failed = 0
            total = len(self._poll_chats)
            try:
                for chat_id in list(self._poll_chats):
                    try:
                        if not await self._poll_once(chat_id):
                            failed += 1
                    except Exception as exc:
                        failed += 1
                        logger.error("FeishuAdapter: poll failed chat={}: {}", chat_id, exc)
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

        A chat's first-ever successful round only seeds the cursor (it never
        replays pre-existing history); a chat with a persisted cursor replays
        the messages newer than it, bounded by the replay window; later rounds
        process messages newer than the cursor, oldest first, deduped by
        message id — the WS path may deliver the same message concurrently,
        and the gateway dedups on the message's idempotency key. Returns
        False when the list call failed (drives the poll loop's backoff); the
        cursor only advances past messages that were delivered or are
        permanently undeliverable, so a failed inbound is retried next round.
        """
        if self._rest_client is None:
            self._rest_client = await asyncio.to_thread(self._build_rest_client)
        replay = chat_id in self._poll_replay
        items = await cursors.list_chat(
            self._rest_client,
            chat_id,
            deep=replay,
            replay_window_s=self._replay_window_s,
            cursor_id=self._poll_cursor.get(chat_id),
            cursor_ms=self._poll_cursor_ms.get(chat_id),
        )
        if items is None:
            return False
        if chat_id not in self._poll_seeded:
            self._poll_seeded.add(chat_id)
            self._restore_owner_open_id(items)
            if not replay:
                # Never polled before: seed only (no history replay), even on
                # an empty window, so the first real message is delivered;
                # a failed round never seeds. Saved cursors replay below.
                message_id, self._poll_cursor_ms[chat_id] = cursors.anchor(items)
                if message_id is not None:
                    self._poll_cursor[chat_id] = message_id
                await self._save_cursor(chat_id)
                return True
        pending, stale = cursors.pending_after(
            items,
            self._poll_cursor.get(chat_id),
            self._poll_cursor_ms.get(chat_id),
            replay=replay,
            replay_window_s=self._replay_window_s,
        )
        if stale:
            logger.warning(
                "FeishuAdapter: replay skipped {} message(s) past the window chat={}",
                len(stale),
                chat_id,
            )
        self._poll_replay.discard(chat_id)
        last = await self._poll_deliver(pending, chat_id)
        if last is None and stale and not pending:
            last = stale[-1].message_id  # nothing replayable: move past the gap
        if last:
            self._poll_cursor[chat_id] = last
            moved = next((cursors.create_ms(i) for i in items if i.message_id == last), None)
            self._poll_cursor_ms[chat_id] = moved or self._poll_cursor_ms.get(chat_id, 0)
            await self._save_cursor(chat_id)
        return True

    async def _restore_cursors(self) -> None:
        """Saved chats are polled again at once; their first round replays."""

        store = getattr(self.core, "cursor_store", None)
        saved: dict[str, tuple[str, int]] = (
            await asyncio.to_thread(store.load_poll, self.channel) if store else {}
        )
        for chat_id, (message_id, create_ms) in saved.items():
            if message_id:
                self._poll_cursor[chat_id] = message_id
            self._poll_cursor_ms[chat_id] = create_ms
        self._poll_replay |= set(saved)
        self._poll_chats |= set(saved)

    async def _save_cursor(self, chat_id: str) -> None:
        store = getattr(self.core, "cursor_store", None)
        if store is not None:
            await asyncio.to_thread(
                store.save_poll,
                self.channel,
                chat_id,
                self._poll_cursor.get(chat_id, ""),
                self._poll_cursor_ms[chat_id],
            )

    def _restore_owner_open_id(self, items: list[Any]) -> None:
        """After a daemon restart the in-memory owner open id is gone;
        restore it from the newest user message in the window so outbound
        notifications do not fail until the user's next message
        (send_to_owner has no chat-id bootstrap path)."""

        if self._last_open_id:
            return
        for item in reversed(items):
            sender = getattr(item, "sender", None)
            if sender is None or getattr(sender, "sender_type", "") != "user":
                continue
            open_id = self._sender_open_id(sender)
            if open_id:
                self._last_open_id = open_id
                return

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
                    logger.exception("FeishuAdapter: poll inbound failed chat={}", chat_id)
                    break  # do not advance past the failure; retry next round
                self._poison_retries.pop(key, None)
                logger.error(
                    "FeishuAdapter: poll inbound failed {} times chat={} msg={}; skipping message",
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

    @staticmethod
    def _sender_open_id(sender: Any) -> str:
        """Extract the sender's open id from either response shape: the WS
        event shape (``sender.sender_id.open_id``) or the ListMessage API
        shape (open id on ``sender.id`` with ``id_type=open_id``)."""
        sender_id = getattr(sender, "sender_id", None)
        open_id = getattr(sender_id, "open_id", "") if sender_id is not None else ""
        if not open_id and getattr(sender, "id_type", "") == "open_id":
            open_id = getattr(sender, "id", "")
        return open_id

    def _normalize_poll_item(self, item: Any) -> InboundMessage | None:
        """Map one listed message to an InboundMessage (same contract as the
        WS event path: p2p text from a user, never our own sends).

        Accepts both response shapes: the WS event shape (``chat_type`` +
        ``sender.sender_id.open_id``) and the ListMessage API shape (no
        ``chat_type`` — the polled chat is a resolved p2p chat by
        construction — and the open id on ``sender.id`` with
        ``id_type=open_id``).
        """
        if not getattr(item, "message_id", ""):
            return None
        # ListMessage responses carry no chat_type; absence means p2p.
        if getattr(item, "chat_type", "") not in ("", "p2p"):
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
        open_id = self._sender_open_id(sender)
        if not open_id:
            return None
        self._last_open_id = open_id
        return InboundMessage(
            channel=self.channel,
            chat_id=open_id,
            text=content,
            message_id=getattr(item, "message_id", None),
            idempotency_key=cursors.idempotency_key(getattr(item, "message_id", None)),
        )

    async def outbound_account_id(self) -> str:
        if not self._config.feishu_app_id or not self._config.feishu_app_secret:
            raise RuntimeError("feishu timeline account is not configured")
        return self._config.feishu_app_id

    async def prepare_timeline(self, text: str) -> PreparedOutboundSend:
        return PreparedOutboundSend(
            adapter_kind=OutboundAdapterKind.FEISHU,
            account_id=await self.outbound_account_id(),
            chunks=tuple(OutboundChunk(text=part) for part in _segment(text, MAX_SEGMENT_CHARS)),
            markdown=False,
        )

    async def send_prepared_outbound(self, chat_id: str, prepared: PreparedOutboundSend) -> None:
        if (
            prepared.adapter_kind != OutboundAdapterKind.FEISHU
            or prepared.account_id != await self.outbound_account_id()
        ):
            raise RuntimeError("feishu prepared account or adapter mismatch")
        self._check_send_ready()
        if self._rest_client is None:
            self._rest_client = await asyncio.to_thread(self._build_rest_client)
        for chunk in prepared.chunks:
            await asyncio.to_thread(self._send_one, self._rest_client, chat_id, chunk.text)
        await self._register_sent_chat(chat_id)

    def _check_send_ready(self) -> None:
        if not self._app_id or not self._app_secret:
            raise RuntimeError("feishu send failed: adapter not configured")
        if self._ws_thread is None or not self._ws_thread.is_alive():
            raise RuntimeError("feishu send failed: adapter not started")

    async def send(
        self,
        chat_id: str,
        text: str,
        *,
        buttons: list[tuple[str, str]] | None = None,
        markdown: bool = False,
    ) -> None:
        """Send a card with buttons, or plain text segmented at the platform cap.

        Markdown is accepted by the shared contract but not rendered.
        """

        del markdown  # platform contract: accepted, not rendered
        self._check_send_ready()
        if self._rest_client is None:
            self._rest_client = await asyncio.to_thread(self._build_rest_client)
        if buttons:
            await asyncio.to_thread(self._send_card, self._rest_client, chat_id, text, buttons)
        else:
            for segment in _segment(text, MAX_SEGMENT_CHARS):
                await asyncio.to_thread(self._send_one, self._rest_client, chat_id, segment)
        # Register the returned p2p chat for polling.
        await self._register_sent_chat(chat_id)

    async def send_to_owner(self, text: str, *, markdown: bool = False) -> None:
        """Send to the last known p2p sender; skip if there is no owner chat."""

        del markdown  # platform contract: accepted, not rendered
        if not self._last_open_id:
            # NotImplementedError: the notify fan-out skips (no retry fixes it; #4964).
            raise NotImplementedError("feishu: no known user chat yet")
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
        # Delivery-count surface (task #4250): one line per API-confirmed send.
        logger.info(
            "feishu send ok chat_id={} message_id={}",
            chat_id,
            getattr(data, "message_id", "") if data is not None else "",
        )

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
        # Delivery-count surface (task #4250): one line per API-confirmed send.
        logger.info(
            "feishu send ok chat_id={} message_id={}",
            chat_id,
            getattr(data, "message_id", "") if data is not None else "",
        )
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
