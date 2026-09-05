"""IM Bridge core — message envelope, command routing, per-channel session
state, and SSE subscription push. Each channel is an adapter plugged into
this core; the core owns the gateway client, the command set (/list /switch
/status /help), and the SSE push of an agent's new messages to the chat that
switched to it. Timeline layout mirrors the frontend (cold load + snapshot
events render through the same filter).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from typing import Any

from services.im_bridge import copy, notice_bridge, push_watchdog
from services.im_bridge.gateway_client import GatewayClient
from services.im_bridge.spawn_menu import SpawnMenuMixin
from services.im_bridge.state import (
    _load_outbox,
    _load_switch_state,
    _OutboxEntry,
    _save_outbox,
    _save_switch_state,
)
from services.im_bridge.types import ChatState, IMAdapter, InboundMessage, Reply, SpawnDraft
from shared.config import settings

_log = logging.getLogger("services.im_bridge.core")

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


_PUSH_LIMIT = 2000  # per-message char cap before splitting


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


# SSE read timeout + enqueue backoff are configurable (task #698 G8);
# 120s only trips on a dead connection (keep-alive ~1/s); the 2+4+8+16+32s
# backoff covers a gateway mid-rollout, then we give up.

# The native "typing" indicator (sendChatAction) lasts ~5 seconds per call, so
# refresh it every 4s while the agent works; give up after 5 minutes so a
# silent agent does not type forever.
_TYPING_INTERVAL_S = 4.0
_TYPING_MAX_S = 300.0


class IMBridgeCore(SpawnMenuMixin):
    """Owns per-channel chat state, command routing, and subscription pushes."""

    def __init__(self, db_pool: Any = None) -> None:
        self.gateway = GatewayClient()
        self.notice_bridge = notice_bridge.NoticeBridge(self, db_pool=db_pool)
        self.adapters: dict[str, IMAdapter] = {}
        self.chats: dict[tuple[str, str], ChatState] = {}
        self._subscriptions: dict[tuple[str, str], asyncio.Task[Any]] = {}
        self._typing_tasks: dict[tuple[str, str], asyncio.Task[Any]] = {}
        self._last_pushed: dict[tuple[str, str, int], str] = {}
        # (channel, chat_id, agent_id) -> newest pushed item_id. Per-chat,
        # not per-agent: two chats switched to the same agent share one
        # snapshot stream, and a shared watermark let the later chat's
        # snapshot advance it past what the earlier chat had pushed.

        self._switch_state = _load_switch_state()
        self._disabled_channels: set[str] = set(settings.services.im_disabled_adapters)
        self._outbox_replay_task: asyncio.Task[Any] | None = None

    def register(self, adapter: IMAdapter) -> None:
        self.adapters[adapter.channel] = adapter

    async def notify_user(self, text: str) -> dict[str, str]:
        """Fan one outbound message out to every loaded adapter's owner chat.

        Used by the ops-alerts pipeline (the gateway POSTs to the daemon's
        ``/send`` RPC): the user gets the alert on whichever IM channels are
        actually connected. A channel that fails (unconfigured, no known
        chat, platform error) is logged and skipped — one broken channel must
        not stop the others."""

        results: dict[str, str] = {}
        for channel, adapter in self.adapters.items():
            try:
                await adapter.send_to_owner(text)
                results[channel] = "ok"
            except NotImplementedError:
                results[channel] = "skipped"
            except Exception as exc:  # fan-out must not break
                _log.warning("notify_user: %s send_to_owner failed: %r", channel, exc)
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
                reply = await self._handle_spawn_menu(state, text)
            elif text.startswith("/"):
                reply = await self._handle_command(state, text)
            else:
                try:
                    reply = await self._handle_chat(state, text)
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
                    await self._enqueue_outbox(state, text)
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

    async def _enqueue_outbox(self, state: ChatState, text: str) -> None:
        """Persist one undeliverable user message and start the replay loop.

        AtLeastOnce: the message stays on disk until the gateway accepts it.
        The Idempotency-Key is minted here and replayed unchanged, so the
        gateway dedups even when a response was lost on the wire."""

        entry = _OutboxEntry(
            id=uuid.uuid4().hex,
            channel=state.channel,
            chat_id=state.chat_id,
            agent_id=state.current_agent_id or 0,
            text=text,
            idempotency_key=uuid.uuid4().hex,
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
        """Drain the outbox with backoff, forever: a failed entry already
        burned the full send backoff inside send_message, so wait it out
        again before touching the gateway. Idle rounds sleep and do nothing
        (created on first enqueue and at daemon start)."""

        while True:
            await self._replay_outbox_once()
            await asyncio.sleep(sum(settings.services.im_send_retry_delays) + 5)

    async def _replay_outbox_once(self) -> None:
        """Try every pending entry once, serially; delivered entries are
        removed, failed ones stay for the next round."""

        entries = _load_outbox()
        if not entries:
            return
        remaining: list[_OutboxEntry] = []
        for entry in entries:
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

    async def _send(self, channel: str, chat_id: str, reply: Reply) -> None:
        adapter = self.adapters.get(channel)
        if adapter is None:
            _log.error("no adapter for channel %s", channel)
            return
        await push_watchdog.send_with_retry(self, channel, chat_id, reply, adapter)

    # -- commands ------------------------------------------------------------

    async def _handle_command(self, state: ChatState, text: str) -> Reply | list[Reply] | None:
        cmd, _, arg = text.partition(" ")
        cmd = cmd.lower()
        if cmd == "/list":
            return await self._cmd_list(state.channel)
        if cmd == "/switch":
            return await self._cmd_switch(state, arg.strip())
        if cmd == "/status":
            return await self._cmd_status(state)
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
        return await self._handle_chat(state, text)

    async def _cmd_list(self, channel: str) -> Reply:
        """Live agents to switch to. On button-capable platforms the text is
        one line and every agent (id + label + status) is a tap target; plain
        channels get the full text list since buttons never render there."""

        agents = await self.gateway.list_agents()
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

    async def _cmd_switch(self, state: ChatState, arg: str) -> Reply | list[Reply]:
        if not arg:
            # user ruling: /switch without an id is an error — the picker
            # lives on /list's tap-to-switch card, not here
            return Reply(copy.SWITCH_USAGE)
        agents = await self.gateway.list_agents()
        target = next(
            (
                a
                for a in agents
                if str(a["agent_id"]) == arg or (a.get("label") or "").lower() == arg.lower()
            ),
            None,
        )
        if target is None:
            return Reply(copy.AGENT_NOT_FOUND.format(arg=arg))
        if target.get("status") not in _LIVE_STATUSES:
            return Reply(
                copy.AGENT_CANNOT_SWITCH.format(
                    agent_id=target["agent_id"], status=target["status"]
                )
            )
        prev = state.current_agent_id
        state.current_agent_id = target["agent_id"]
        self._persist_switch(state)
        self._ensure_subscription(state, prev_agent=prev)
        # Raw timeline mixes dialog items with non-dialog ones (agent_updated,
        # task events...), so fetch a wider window and keep the most recent
        # 5 dialog messages (user feedback: replay showed only 2).
        items = await self.gateway.get_timeline(target["agent_id"], limit=20)
        msgs = [it for it in items if _is_dialog_item(it)][-5:]
        replies: list[Reply] = [
            Reply(
                copy.SWITCHED_TO.format(
                    agent_id=target["agent_id"], label=target.get("label") or copy.UNNAMED_LABEL
                )
                if target.get("label")
                else copy.SWITCHED_TO_UNNAMED.format(agent_id=target["agent_id"])
            )
        ]
        if not msgs:
            replies.append(Reply(copy.NO_MESSAGES_YET))
            return replies
        # record push watermark so the subscription only sends what's new
        self._last_pushed[(state.channel, state.chat_id, target["agent_id"])] = msgs[-1]["item_id"]
        # one message per item — never a wall of concatenated text
        for it in reversed(msgs):
            replies.append(Reply(_render_item(it, target["agent_id"]), markdown=True))
        return replies

    async def _cmd_status(self, state: ChatState) -> Reply:
        if state.current_agent_id is None:
            return Reply(copy.NO_AGENT_SWITCHED)
        agents = await self.gateway.list_agents()
        a = next((x for x in agents if x["agent_id"] == state.current_agent_id), None)
        if a is None:
            state.current_agent_id = None
            self._persist_switch(state)
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

    async def _handle_chat(self, state: ChatState, text: str) -> Reply | None:
        if state.current_agent_id is None:
            return Reply(copy.NO_AGENT_SWITCHED)
        # Replies arrive via SSE push — make sure the subscription exists even
        # when the daemon restarted since the last /switch (it is memory-only
        # and was lost; without this the agent replies but the user never
        # receives them, Task #804).
        self._ensure_subscription(state)
        self._start_typing(state)
        await self.gateway.send_message(state.current_agent_id, text)
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
        """Push one fresh dialog item: the agent's first text output stops
        the typing indicator, then lands as a message like everything else."""

        if it.get("kind") == "agent_chat":
            self._stop_typing(state)
        await self._send(
            state.channel, state.chat_id, Reply(_render_item(it, agent_id), markdown=True)
        )

    # -- subscription push ----------------------------------------------------

    async def restore_subscriptions(self) -> None:
        """Rebuild SSE subscriptions from switch_state (Task #804); channels
        disabled via AVA_IM_DISABLED_ADAPTERS get none (in-memory subs die
        with the daemon)."""
        for key, agent_id in self._switch_state.items():
            channel, sep, chat_id = key.partition(":")
            if not sep or not channel or not chat_id:
                continue
            state = self._get_or_create_state(channel, chat_id)
            if state.current_agent_id != agent_id:
                state.current_agent_id = agent_id
            self._ensure_subscription(state)
            await asyncio.sleep(0)  # let the subscription task spin up

    def _ensure_subscription(self, state: ChatState, prev_agent: int | None = None) -> None:
        if state.channel in self._disabled_channels:
            return  # channel disabled (AVA_IM_DISABLED_ADAPTERS): no pushes
        key = (state.channel, state.chat_id)
        existing = self._subscriptions.get(key)
        if existing is not None and not existing.done():
            if state.current_agent_id == prev_agent:
                return  # unchanged — nothing to do
            existing.cancel()  # switched to another agent: restart the stream
        task = asyncio.create_task(self._subscription_loop(key, state))
        self._subscriptions[key] = task

    async def _subscription_loop(self, key: tuple[str, str], state: ChatState) -> None:
        # Escalate INFO->WARNING after 12 consecutive reconnect failures
        # (~1 min at the 5s retry): one drop per gateway restart is expected.
        _sse_reconnect_warn_after = 12
        failures = 0
        while True:
            agent_id = state.current_agent_id
            if agent_id is None:
                return
            try:
                async for event in self.gateway.stream_events(agent_id):
                    if event.get("role") == "timeline_snapshot":
                        await self._push_snapshot(key, state, event)
                    failures = 0  # a live event stream is the reset
            except asyncio.CancelledError:
                return
            except Exception:
                failures += 1
                # stdlib %-style (a loguru '{}' placeholder here used to raise
                # TypeError inside this except block, which the surrounding try
                # does not catch — the reconnect loop and with it all pushes
                # died, Task #1032). suppress: a logging bug must never kill
                # the loop.
                with contextlib.suppress(Exception):
                    level = _log.warning if failures >= _sse_reconnect_warn_after else _log.info
                    level("sse loop error, reconnecting in 5s (x%d)", failures, exc_info=True)
                await asyncio.sleep(5)

    async def _push_snapshot(
        self, _key: tuple[str, str], state: ChatState, event: dict[str, Any]
    ) -> None:
        if state.current_agent_id is None:
            return
        agent_id = state.current_agent_id
        raw_items: list[Any] = event.get("items", [])
        items: list[dict[str, Any]] = [it for it in raw_items if _is_dialog_item(it)]
        if not items:
            return
        items.sort(key=lambda it: _item_key(str(it.get("item_id", "0.0"))))
        watermark_key = (*_key, agent_id)  # (channel, chat_id, agent_id)
        watermark = self._last_pushed.get(watermark_key)
        # Numeric comparison via _item_key (same key the sort uses): the old
        # string compare called '9.5' > '10.1' false, so the first message
        # across a magnitude boundary silently stopped all pushes (Task #1032).
        fresh = [
            it
            for it in items
            if watermark is None or _item_key(str(it["item_id"])) > _item_key(watermark)
        ]
        if not fresh:
            return
        self._last_pushed[watermark_key] = str(fresh[-1]["item_id"])
        for it in fresh:
            await self._deliver_item(state, it, agent_id)


def _truncate(text: str, limit: int) -> str:
    """Clip to ``limit`` chars, ASCII ellipsis when cut."""

    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _spawn_button(draft: SpawnDraft) -> tuple[str, str]:
    """The summary [Spawn] button every menu layer carries: the current
    selections (or "default") so the layer is completable as-is (user
    ruling: the button shows the selection summary)."""

    preset = draft.preset_label if draft.preset_id is not None else "default"
    summary = f"Spawn: {preset} / {draft.model or 'default'} / {draft.effort or 'default'}"
    return (summary, "spawn:go")


def _item_key(item_id: str) -> tuple[int, int]:
    try:
        msg_idx, block_idx = item_id.split(".", 1)
        return (int(msg_idx), int(block_idx or 0))
    except ValueError:
        return (0, 0)


def _render_item(it: dict[str, Any], agent_id: int | None = None) -> str:
    """One pushed line: the human's own words or the agent's text output,
    tagged so the reader always knows who is speaking (the user's format:
    ``[User]`` / ``[Ava #<id>]``)."""

    payload = (it.get("payload") or "").strip()
    kind = it.get("kind", "")
    if kind == "inbound_chat":
        return f"[User] {payload}"
    if kind == "agent_chat":
        who = f"Ava #{agent_id}" if agent_id is not None else "Ava"
        return f"[{who}] {payload}"
    return f"[{kind}] {payload}"
