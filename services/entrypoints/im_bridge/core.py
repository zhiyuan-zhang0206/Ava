"""IM command routing, chat selection and durable timeline acceptance."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from functools import partial
from typing import Any, Literal

from base.deploy.maintenance import admission
from services.entrypoints.im_bridge import copy, notice_bridge, push_watchdog
from services.entrypoints.im_bridge.config import ImBridgeConfig
from services.entrypoints.im_bridge.cursor_store import (
    CursorStore,
    PushWatermark,
)
from services.entrypoints.im_bridge.cursor_store import (
    item_key as _item_key,
)
from services.entrypoints.im_bridge.gateway_client import GatewayClient
from services.entrypoints.im_bridge.outbound_store import IMOutboxStore
from services.entrypoints.im_bridge.outbound_types import TimelineAcceptance
from services.entrypoints.im_bridge.outbound_worker import IMOutboxWorker
from services.entrypoints.im_bridge.spawn_menu import SpawnMenuMixin
from services.entrypoints.im_bridge.state import (
    _load_outbox,
    _load_switch_state,
    _OutboxEntry,
    _save_outbox,
    _save_switch_state,
)
from services.entrypoints.im_bridge.timeline_acceptance import (
    SelectionAdmission,
    accept_timeline,
    apply_account_selection,
    hold_selection,
    register_adapter,
    render_item,
    selection_account,
    sync_selection,
)
from services.entrypoints.im_bridge.timeline_acceptance import truncate as _truncate
from services.entrypoints.im_bridge.types import (
    AgentRow,
    ChatState,
    IMAdapter,
    InboundMessage,
    Reply,
    SendNotStartedError,
    parse_command,
)

_log = logging.getLogger("services.entrypoints.im_bridge.core")

# Kinds that count as "dialog" for the IM surface: the human's own messages
# and the agent's text output. Tool execution / reasoning / system markers /
# other agents' messages are never pushed.
_DIALOG_KINDS = frozenset({"inbound_chat", "agent_chat"})
_USER_SOURCE = "user"

# Statuses the IM surface treats as "live".
_LIVE_STATUSES = ("running", "idling")


def _display_status(status: str) -> str:
    """Pass-through kept so the display call sites read uniformly."""

    return status


def _is_dialog_item(it: dict[str, Any]) -> bool:
    """The default push filter: user-originated messages + agent text output.

    ``inbound_chat`` rows carry their envelope ``source`` — only the human's
    own (``user``) passes; watcher/schedule/peer-agent inbounds are dropped.
    """

    kind = it.get("kind")
    if kind not in _DIALOG_KINDS:
        return False
    if kind == "inbound_chat":
        return it.get("source") == _USER_SOURCE
    return True


# SSE timeout and inbound enqueue backoff come from the service config slice.

# Refresh Telegram's ~5-second typing indicator every 4s, for at most 5 minutes.
_TYPING_INTERVAL_S = 4.0
_TYPING_MAX_S = 300.0


_render_item = render_item  # Compatibility for the existing renderer consumer.


class IMBridgeCore(SpawnMenuMixin):
    """Owns per-channel chat state, command routing, and subscription pushes."""

    def __init__(self, config: ImBridgeConfig, gateway: GatewayClient, db_pool: Any = None) -> None:
        self.config = config
        self.gateway = gateway
        self.cursor_store = CursorStore(db_pool)
        self.outbound_store = IMOutboxStore(db_pool)
        self.notice_bridge = notice_bridge.NoticeBridge(self, config, db_pool=db_pool)
        self.adapters: dict[str, IMAdapter] = {}
        self.outbound_worker = IMOutboxWorker(self.outbound_store, self.adapters)
        self.chats: dict[tuple[str, str], ChatState] = {}
        self._selection_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._subscriptions: dict[tuple[str, str], asyncio.Task[Any]] = {}
        self._typing_tasks: dict[tuple[str, str], asyncio.Task[Any]] = {}
        self._last_pushed: dict[tuple[str, str, int], PushWatermark] = {}
        # Post-commit memory cache; the cursor row owns acceptance and selection.

        self._switch_state = _load_switch_state()
        self._disabled_channels: set[str] = set(config.im_disabled_adapters)
        self._outbox_replay_task: asyncio.Task[Any] | None = None

    def register(self, adapter: IMAdapter) -> None:
        register_adapter(self, adapter)

    async def notify_user(self, text: str) -> dict[str, str]:
        """Fan ops notifications to loaded owner chats, isolating each result.

        This separate producer keeps its existing immediate send contract.
        Only SendNotStartedError permits one bounded retry; uncertain sends stop.
        """

        results: dict[str, str] = {}
        for channel, adapter in self.adapters.items():
            try:
                await adapter.send_to_owner(text)
                results[channel] = "ok"
            except NotImplementedError:
                results[channel] = "skipped"
            except SendNotStartedError as exc:
                _log.warning("notify_user: %s send_to_owner failed before send: %r", channel, exc)
                try:
                    # partial, not a lambda: this iteration's adapter is bound
                    # now, so the retry call can never read a loop variable late.
                    await push_watchdog.retry_once_after_backoff(
                        partial(adapter.send_to_owner, text), self.config
                    )
                    results[channel] = "ok"
                except Exception as retry_exc:
                    _log.warning(
                        "notify_user: %s send_to_owner retry failed: %r", channel, retry_exc
                    )
                    results[channel] = f"error: {type(retry_exc).__name__}"
            except Exception as exc:  # isolate this uncertain channel from the fan-out
                _log.warning("notify_user: %s outcome uncertain; no retry: %r", channel, exc)
                results[channel] = f"error: {type(exc).__name__}"
        return results

    # -- inbound -------------------------------------------------------------

    def _state_key(self, channel: str, chat_id: str) -> str:
        return f"{channel}:{chat_id}"

    def _get_or_create_state(self, channel: str, chat_id: str) -> ChatState:
        key = (channel, chat_id)
        state = self.chats.get(key)
        if state is None:
            state = ChatState(channel, chat_id)
            state.current_agent_id = self._switch_state.get(self._state_key(channel, chat_id))
            self.chats[key] = state
        return state

    def _persist_switch(self, state: ChatState) -> None:
        """Write the chat's current agent to disk (or drop it when cleared)."""

        key = self._state_key(state.channel, state.chat_id)
        if state.current_agent_id is None:
            self._switch_state.pop(key, None)
        else:
            self._switch_state[key] = state.current_agent_id
        _save_switch_state(self._switch_state)

    async def handle_inbound(self, msg: InboundMessage) -> None:
        """Normalize entry point from adapters. Replies via the originating
        adapter; returns nothing (agent replies arrive via SSE push)."""
        try:
            nb = self.notice_bridge
            if msg.text.startswith("notice:"):
                hint = await nb.handle_callback(msg.chat_id, msg.text)
            elif msg.text.startswith("/notice"):
                cmd = msg.text[len("/notice") :].strip()
                hint = await nb.list_queue() if cmd == "list" else nb.cmd_notice(cmd)
            else:
                hint = await nb.handle_inbound(msg.chat_id, msg.text)
            if hint is not None:
                await self._send(msg.channel, msg.chat_id, Reply(hint))
                return
            state = self._get_or_create_state(msg.channel, msg.chat_id)
            text = msg.text.strip()
            if text.startswith("spawn:"):
                # inline-keyboard navigation of the /spawn menu (only
                # callbacks carry this prefix; typed text never does)
                reply = await self._handle_spawn_menu(
                    state, text, idempotency_key=msg.idempotency_key
                )
            elif text.startswith("/"):
                reply = await self._handle_command(
                    state,
                    text,
                    msg.idempotency_key,
                    replay_id=msg.idempotency_key or msg.message_id or uuid.uuid4().hex,
                )
            else:
                try:
                    reply = await self._handle_chat(state, text, msg.idempotency_key)
                except Exception:
                    # Gateway enqueue failed after every retry — the platform
                    # offset has moved, so only the outbox can save this
                    # message (Task #1032: it used to be silently dropped).
                    _log.exception(
                        "chat enqueue failed channel=%s chat=%s — outboxing",
                        msg.channel,
                        msg.chat_id,
                    )
                    self._stop_typing(state)
                    await self._enqueue_outbox(state, text, msg.idempotency_key)
                    await self._send(msg.channel, msg.chat_id, Reply(copy.QUEUED_NOTICE))
                    return
            if reply:
                replies = reply if isinstance(reply, list) else [reply]
                for r in replies:
                    await self._send(msg.channel, msg.chat_id, r)
                await push_watchdog.hint_recovered(self, msg)
        except Exception:
            _log.exception("handle_inbound failed channel=%s chat=%s", msg.channel, msg.chat_id)

    # -- inbound outbox (Task #1032) -----------------------------------------

    async def _enqueue_outbox(
        self, state: ChatState, text: str, idempotency_key: str | None = None
    ) -> None:
        """Persist one undeliverable user message and start the replay loop.

        AtLeastOnce: the message stays on disk until the gateway accepts it.
        The Idempotency-Key is the platform's own (when the adapter supplies
        one) or minted here, and replayed unchanged, so the gateway dedups
        even when a response was lost on the wire."""

        entry = _OutboxEntry(
            id=uuid.uuid4().hex,
            channel=state.channel,
            chat_id=state.chat_id,
            agent_id=state.current_agent_id or 0,
            text=text,
            idempotency_key=idempotency_key or uuid.uuid4().hex,
            enqueued_at=time.time(),
        )
        entries = _load_outbox()
        entries.append(entry)
        _save_outbox(entries)
        _log.warning("im_bridge: outboxed message id=%s (enqueue failed)", entry.id)
        self.ensure_outbox_replay()

    def ensure_outbox_replay(self) -> None:
        """Start the replay loop if it is not already running (idempotent).
        Called on first outbox enqueue and at daemon startup — a restart
        must drain what the previous process left behind."""

        if self._outbox_replay_task is None or self._outbox_replay_task.done():
            self._outbox_replay_task = asyncio.create_task(self._outbox_replay_loop())

    async def _outbox_replay_loop(self) -> None:
        """Drain the existing inbound journal through the keyed gateway API."""

        # quiesce-exempt: replays a local file outbox through the gateway API; no database
        while True:
            await self._replay_outbox_once()
            await asyncio.sleep(sum(self.config.im_send_retry_delays) + 5)

    async def _replay_outbox_once(self) -> None:
        """Try every pending entry once, serially; delivered entries are
        removed, failed ones stay for the next round."""

        entries = _load_outbox()
        if not entries:
            return
        remaining: list[_OutboxEntry] = []
        for entry in entries:
            if entry.channel == "weixin":
                _log.warning(
                    "im_bridge: legacy Weixin journal held reason=unbound_provider_account"
                )
                remaining.append(entry)
                continue
            try:
                await self.gateway.send_message(
                    entry.agent_id,
                    entry.text,
                    idempotency_key=entry.idempotency_key,
                )
            except Exception:
                _log.exception("im_bridge: outbox replay failed id=%s (kept for retry)", entry.id)
                remaining.append(entry)
            else:
                _log.info("im_bridge: outbox replay delivered id=%s", entry.id)
        _save_outbox(remaining)

    async def _send(
        self, channel: str, chat_id: str, reply: Reply, *, adapter: IMAdapter | None = None
    ) -> None:
        adapter = adapter or self.adapters.get(channel)
        if adapter is None:
            _log.error("no adapter for channel %s", channel)
            return
        await push_watchdog.send_with_retry(self, channel, chat_id, reply, adapter)

    # -- commands ------------------------------------------------------------

    async def _handle_command(
        self,
        state: ChatState,
        text: str,
        idempotency_key: str | None = None,
        *,
        replay_id: str | None = None,
        selection_admission: SelectionAdmission | None = None,
    ) -> Reply | list[Reply] | None:
        cmd, arg = parse_command(text)
        if cmd == "/list":
            return await self._cmd_list(state.channel)
        if cmd == "/switch":
            return await self._cmd_switch(
                state, arg.strip(), replay_id=replay_id, selection_admission=selection_admission
            )
        if cmd == "/status":
            return await self._cmd_status(state, selection_admission=selection_admission)
        if cmd == "/spawn":
            # args are ignored — /spawn is a pure menu (user ruling)
            return await self._cmd_spawn(state)
        if cmd == "/commands":
            return await self._cmd_commands(state.channel)
        if cmd == "/help":
            return self._cmd_help()
        # Unknown "/..." commands pass through to the current agent — the
        # gateway's claim node expands registered ones (skill-as-command,
        # prompt templates) exactly like the web composer does.
        return await self._handle_chat(state, text, idempotency_key)

    async def _cmd_list(self, channel: str) -> Reply:
        """Live agents to switch to. On button-capable platforms the text is
        one line and every agent (id + label + status) is a tap target; plain
        channels get the full text list since buttons never render there."""

        agents: list[AgentRow] = []
        before_id = None
        while True:
            page = await self.gateway.list_agents(scope="live", before_id=before_id)
            agents.extend(page["agents"])
            before_id = page["next_cursor"]
            if before_id is None:
                break
        alive = sorted(
            (a for a in agents if a.get("status") in _LIVE_STATUSES),
            key=lambda a: a["agent_id"],
        )
        if not alive:
            return Reply(copy.NO_LIVE_AGENTS)
        adapter = self.adapters.get(channel)
        if adapter is not None and adapter.can_buttons:
            buttons = [
                (
                    f"{a['agent_id']} {a.get('label') or copy.UNNAMED_LABEL} [{_display_status(a['status'])}]",
                    f"/switch {a['agent_id']}",
                )
                for a in alive
            ]
            return Reply(copy.LIVE_AGENTS_TITLE_BUTTONS, buttons=buttons)
        lines = [
            f"{a['agent_id']}  {a.get('label') or copy.UNNAMED_LABEL}  [{_display_status(a['status'])}]"
            for a in alive
        ]
        return Reply(copy.LIVE_AGENTS_TITLE + "\n" + "\n".join(lines))

    async def _find_switch_target(self, arg: str) -> AgentRow | None:
        """IDs are direct lookups; labels match exactly within scoped search pages."""
        if arg.isdecimal():
            return await self.gateway.get_agent(int(arg))
        scopes: tuple[Literal["live", "terminated"], ...] = ("live", "terminated")
        for scope in scopes:
            before_id = None
            while True:
                page = await self.gateway.list_agents(scope=scope, query=arg, before_id=before_id)
                for agent in page["agents"]:
                    if (agent["label"] or "").casefold() == arg.casefold():
                        return agent
                before_id = page["next_cursor"]
                if before_id is None:
                    break
        return None

    async def _cmd_switch(
        self,
        state: ChatState,
        arg: str,
        *,
        replay_id: str | None = None,
        selection_admission: SelectionAdmission | None = None,
    ) -> Reply | list[Reply]:
        async with self._selection_lock(state):
            return await self._cmd_switch_locked(
                state, arg, replay_id=replay_id, selection_admission=selection_admission
            )

    def _selection_lock(self, state: ChatState) -> asyncio.Lock:
        return self._selection_locks.setdefault((state.channel, state.chat_id), asyncio.Lock())

    def _apply_selection(self, state: ChatState, selected: int | None) -> None:
        previous = state.current_agent_id
        state.current_agent_id = selected
        self._persist_switch(state)
        if selected is None:
            task = self._subscriptions.pop((state.channel, state.chat_id), None)
            if task is not None:
                task.cancel()
        else:
            self._ensure_subscription(state, prev_agent=previous)

    def _hold_selection(self, state: ChatState) -> None:
        hold_selection(self, state)

    async def _sync_selection(self, state: ChatState) -> int | None:
        return await sync_selection(self, state)

    async def _cmd_switch_locked(
        self,
        state: ChatState,
        arg: str,
        *,
        replay_id: str | None = None,
        selection_admission: SelectionAdmission | None = None,
    ) -> Reply | list[Reply]:
        if not arg:
            # user ruling: /switch without an id is an error — the picker
            # lives on /list's tap-to-switch card, not here
            return Reply(copy.SWITCH_USAGE)
        if admission.quiesced():
            raise RuntimeError("IM switch acceptance is held during maintenance")
        replay_id = replay_id or uuid.uuid4().hex
        account = await selection_account(self, state, selection_admission)
        recovered = await asyncio.to_thread(
            self.outbound_store.lookup_replay,
            state.channel,
            account,
            state.chat_id,
            replay_id,
            arg,
        )
        if recovered is not None:
            apply_account_selection(self, state, recovered.selected_agent_id, selection_admission)
            return []
        target = await self._find_switch_target(arg)
        if target is None:
            return Reply(copy.AGENT_NOT_FOUND.format(arg=arg))
        if target.get("status") not in _LIVE_STATUSES:
            return Reply(
                copy.AGENT_CANNOT_SWITCH.format(
                    agent_id=target["agent_id"], status=target["status"]
                )
            )
        # Raw timeline mixes dialog items with non-dialog ones (agent_updated,
        # task events...), so fetch a wider window and keep the most recent
        # `replay` dialog messages (user feedback: replay showed only 2).
        window = self.config.im_bridge_timeline_window
        replay = self.config.im_bridge_replay_messages
        items = await self.gateway.get_timeline(target["agent_id"], limit=window)
        msgs = [it for it in items if _is_dialog_item(it)][-replay:]
        replies: list[Reply] = [
            Reply(
                copy.SWITCHED_TO.format(
                    agent_id=target["agent_id"], label=target.get("label") or copy.UNNAMED_LABEL
                )
                if target.get("label")
                else copy.SWITCHED_TO_UNNAMED.format(agent_id=target["agent_id"])
            )
        ]
        acceptance = await self._accept_timeline(
            state,
            target["agent_id"],
            list(reversed(msgs)),
            replay_id=replay_id,
            switch_arg=arg,
            selection_admission=selection_admission,
        )
        if acceptance.blocked:
            raise ValueError("switch replay requires durable timeline source identities")
        apply_account_selection(self, state, acceptance.selected_agent_id, selection_admission)
        if not msgs:
            replies.append(Reply(copy.NO_MESSAGES_YET))
        return replies

    async def _cmd_status(
        self, state: ChatState, *, selection_admission: SelectionAdmission | None = None
    ) -> Reply:
        async with self._selection_lock(state):
            return await self._cmd_status_locked(state, selection_admission=selection_admission)

    async def _cmd_status_locked(
        self, state: ChatState, *, selection_admission: SelectionAdmission | None = None
    ) -> Reply:
        if selection_admission is None:
            await self._sync_selection(state)
        if state.current_agent_id is None:
            return Reply(copy.NO_AGENT_SWITCHED)
        a = await self.gateway.get_agent(state.current_agent_id)
        if a is None:
            account = (
                selection_admission.account_id
                if selection_admission is not None
                else await self.adapters[state.channel].outbound_account_id()
            )
            await asyncio.to_thread(
                self.outbound_store.clear_selection,
                state.channel,
                account,
                state.chat_id,
                state.current_agent_id,
                guard=selection_admission.guard if selection_admission is not None else None,
            )
            if selection_admission is None:
                await self._sync_selection(state)
            return Reply(copy.CURRENT_AGENT_GONE)
        label = a.get("label") or copy.UNNAMED_LABEL
        lines = [
            copy.STATUS_DETAIL_LINE.format(agent_id=a["agent_id"], label=label),
            copy.STATUS_STATE_LINE.format(status=a.get("status")),
        ]
        for key, name in copy.STATUS_LABELS.items():
            if a.get(key) is not None:
                lines.append(f"{name}: {a[key]}")
        return Reply("\n".join(lines))

    def _cmd_help(self) -> Reply:
        """The IM's own commands. /commands carries the full Ava
        slash-command catalog (skills and prompt templates); the persistent
        command menu (setMyCommands) mirrors the IM set, and tapping one
        autofills the input box."""

        return Reply(copy.HELP_TEXT)

    async def _cmd_commands(self, channel: str) -> Reply | list[Reply]:
        """/commands — the Ava slash-command catalog: every active skill is
        a command (``/audio-transcribe …``), plus project/user/plugin prompt
        templates. On button channels every command is a tap target (the tap
        runs it, so the agent can ask for the missing instruction);
        descriptions are truncated everywhere (Telegram renders the em-dash
        and long lines poorly)."""

        commands = await self.gateway.list_commands()
        if not commands:
            return Reply(copy.NO_COMMANDS_REGISTERED)
        lines = [
            copy.COMMANDS_HEADER,
            "",
            copy.COMMANDS_INTRO.format(count=len(commands)),
            *[f"/{c['name']}: {_truncate(c.get('description') or '', 60)}" for c in commands],
        ]
        text = "\n".join(lines)
        adapter = self.adapters.get(channel)
        if adapter is not None and adapter.can_buttons:
            buttons = [(f"/{c['name']}", f"/{c['name']}") for c in commands]
            return Reply(text, buttons=buttons)
        return Reply(text)

    async def _handle_chat(
        self, state: ChatState, text: str, idempotency_key: str | None = None
    ) -> Reply | None:
        async with self._selection_lock(state):
            return await self._handle_chat_locked(state, text, idempotency_key)

    async def _handle_chat_locked(
        self, state: ChatState, text: str, idempotency_key: str | None = None
    ) -> Reply | None:
        await self._sync_selection(state)
        if state.current_agent_id is None:
            return Reply(copy.NO_AGENT_SWITCHED)
        # Replies arrive via SSE push — make sure the subscription exists even
        # when the daemon restarted since the last /switch (it is memory-only
        # and was lost; without this the agent replies but the user never
        # receives them, Task #804).
        self._ensure_subscription(state)
        self._start_typing(state)
        await self.gateway.send_message(
            state.current_agent_id, text, idempotency_key=idempotency_key
        )
        return None  # the reply arrives via subscription push

    # -- typing indicator ------------------------------------------------------

    def _start_typing(self, state: ChatState) -> None:
        """Show the platform's native \"typing\" indicator while the agent
        works: a background task refreshes it until the reply arrives (or a
        timeout), then the first pushed agent reply stops it."""

        adapter = self.adapters.get(state.channel)
        if adapter is None or not adapter.can_type:
            return
        key = (state.channel, state.chat_id)
        existing = self._typing_tasks.get(key)
        if existing is not None and not existing.done():
            return  # already typing in this chat
        self._typing_tasks[key] = asyncio.create_task(self._typing_loop(key, state, adapter))

    async def _typing_loop(
        self, key: tuple[str, str], state: ChatState, adapter: IMAdapter
    ) -> None:
        deadline = time.monotonic() + _TYPING_MAX_S
        try:
            while time.monotonic() < deadline:
                try:
                    await adapter.typing(state.chat_id)
                except Exception:
                    # cosmetic feature — a failing indicator gives up quietly
                    _log.warning("typing failed channel=%s chat=%s", state.channel, state.chat_id)
                    return
                await asyncio.sleep(_TYPING_INTERVAL_S)
        finally:
            self._typing_tasks.pop(key, None)

    def _stop_typing(self, state: ChatState) -> None:
        key = (state.channel, state.chat_id)
        task = self._typing_tasks.pop(key, None)
        if task is not None:
            task.cancel()

    async def _deliver_item(self, state: ChatState, it: dict[str, Any], agent_id: int) -> None:
        """Push one fresh dialog item as a message."""

        await self._accept_timeline(state, agent_id, [it])

    # -- subscription push ----------------------------------------------------

    async def restore_subscriptions(self) -> None:
        """Restore canonical selection and bootstrap the legacy JSON cache once."""
        self._last_pushed.update(await asyncio.to_thread(self.cursor_store.load_push))
        legacy = {
            (channel, chat): agent
            for key, agent in self._switch_state.items()
            for channel, separator, chat in [key.partition(":")]
            if separator and channel and chat
        }
        candidates = await asyncio.to_thread(self.outbound_store.restore_candidates, legacy)
        for (channel, chat_id), agent_id in candidates.items():
            if channel not in self.adapters or channel in self._disabled_channels:
                continue
            state = self._get_or_create_state(channel, chat_id)
            state.current_agent_id = agent_id
            try:
                async with self._selection_lock(state):
                    await self._sync_selection(state)
            except Exception as exc:
                _log.warning(
                    "selection restore held channel=%s class=%s", channel, type(exc).__name__
                )
                continue
            if state.current_agent_id is not None:
                self._ensure_subscription(state, catch_up=True)
            await asyncio.sleep(0)

    def _ensure_subscription(
        self, state: ChatState, prev_agent: int | None = None, *, catch_up: bool = False
    ) -> None:
        if state.channel in self._disabled_channels:
            return  # channel disabled (AVA_IM_DISABLED_ADAPTERS): no pushes
        key = (state.channel, state.chat_id)
        existing = self._subscriptions.get(key)
        if existing is not None and not existing.done():
            if state.current_agent_id == prev_agent:
                return  # unchanged — nothing to do
            existing.cancel()  # switched to another agent: restart the stream
        task = asyncio.create_task(self._subscription_loop(key, state, catch_up=catch_up))
        self._subscriptions[key] = task

    async def _subscription_loop(
        self, key: tuple[str, str], state: ChatState, *, catch_up: bool = False
    ) -> None:
        _sse_reconnect_warn_after = 12
        failures = 0
        # quiesce-exempt: an SSE reconnect loop against the gateway; a cursor is written only when an event arrives
        while True:
            agent_id = state.current_agent_id
            if agent_id is None:
                return
            try:
                if catch_up:
                    await self._catch_up(key, state, agent_id)
                catch_up = True  # every reconnect missed whatever the gap carried
                async for event in self.gateway.stream_events(agent_id):
                    if event.get("role") == "timeline_snapshot":
                        await self._push_snapshot(key, state, event)
                    failures = 0  # a live event stream is the reset
            except asyncio.CancelledError:
                return
            except Exception:
                failures += 1
                # Report the first failure and sustained reconnect failures.
                if failures == 1 or failures >= _sse_reconnect_warn_after:
                    _log.warning(
                        "sse loop error, reconnecting in 5s (x%d)", failures, exc_info=True
                    )
                else:
                    _log.info("sse loop error, reconnecting in 5s (x%d)", failures)
                await asyncio.sleep(5)

    async def _push_snapshot(
        self, key: tuple[str, str], state: ChatState, event: dict[str, Any]
    ) -> None:
        del event
        # SSE contains live reducer snapshots, not a checkpoint commit receipt.
        # It only wakes a read through the committed timeline owner.
        if state.current_agent_id is not None:
            await self._catch_up(key, state, state.current_agent_id)

    async def _catch_up(self, key: tuple[str, str], state: ChatState, agent_id: int) -> None:
        async with self._selection_lock(state):
            await self._catch_up_locked(key, state, agent_id)

    async def _catch_up_locked(self, key: tuple[str, str], state: ChatState, agent_id: int) -> None:
        """Accept committed tail through the locked cursor owner."""

        items = await self.gateway.get_timeline(agent_id)
        if state.current_agent_id == agent_id:
            await self._push_items(key, state, items)

    async def _push_items(
        self, key: tuple[str, str], state: ChatState, raw_items: list[Any]
    ) -> None:
        if state.current_agent_id is None:
            return
        agent_id = state.current_agent_id
        items = [it for it in raw_items if _is_dialog_item(it)]
        if not items:
            return
        items.sort(key=lambda it: _item_key(str(it.get("item_id", "0.0"))))
        acceptance = await self._accept_timeline(state, agent_id, items)
        if acceptance.intent_ids and any(it.get("kind") == "agent_chat" for it in items):
            self._stop_typing(state)
        if acceptance.blocked:
            _log.warning(
                "timeline acceptance held for unqualified source channel=%s chat=%s agent=%s",
                *key,
                agent_id,
            )

    async def _accept_timeline(
        self,
        state: ChatState,
        agent_id: int,
        items: list[dict[str, Any]],
        *,
        replay_id: str = "",
        switch_arg: str = "",
        selection_admission: SelectionAdmission | None = None,
    ) -> TimelineAcceptance:
        return await accept_timeline(
            self,
            state,
            agent_id,
            items,
            replay_id=replay_id,
            switch_arg=switch_arg,
            selection_admission=selection_admission,
        )

    async def poll_timeline_outbound(self) -> None:
        """A committed-tail pull covers an SSE event emitted before its commit."""
        for key, state in list(self.chats.items()):
            if state.current_agent_id is None:
                continue
            try:
                async with self._selection_lock(state):
                    selected = await self._sync_selection(state)
                    if selected is not None:
                        await self._catch_up_locked(key, state, selected)
            except Exception as exc:
                _log.warning(
                    "committed timeline pull failed channel=%s class=%s",
                    state.channel,
                    type(exc).__name__,
                )
        await self.outbound_worker.run_once()
