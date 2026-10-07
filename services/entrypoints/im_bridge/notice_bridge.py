"""Telegram notice bridge (Task #884).

Reads new fleet notices (an agent's ``ava.ui.notify`` -> agent_notices rows)
and durably accepts normal pushes for the Telegram owner chat — a channel independent of the /switch conversation flow. Pushed notices
carry inline buttons:

- [Reply] (both kinds, Task #1061 made FYI answerable too) arms a reply-mode
  window (``services.im_bridge_notice_reply_window_seconds``, default 5
  minutes): plain-text messages in that window resolve the notice with that
  text as the answer, so chatting with agents and answering notices never
  collide; /cancel exits early.
- [OK] / [Close] resolve an FYI as read or dismiss a response-required notice.

A ``/notice filter`` command restricts what gets pushed (minimum priority
and/or a single agent). Filters and a diagnostic cursor persist under ``$AVA_HOME/state/im_bridge/``;
normal-poll source receipts and the immutable cutover live in Postgres.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from base.db.transaction import write_transaction
from base.paths import ava_home
from services.entrypoints.im_bridge import copy
from services.entrypoints.im_bridge.config import ImBridgeConfig
from services.entrypoints.im_bridge.notice_poll_store import NoticePollStore
from services.entrypoints.im_bridge.outbound_types import (
    NoticePollImportReason,
    OutboundIntent,
    OutboundSource,
    OutboundSourceKind,
)

_log = logging.getLogger("services.entrypoints.im_bridge.notice_bridge")

_CB_REPLY = "notice:reply:"
_CB_READ = "notice:read:"
_CB_DISMISS = "notice:dismiss:"
_CB_LIST = "notice:list"

_PRIORITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


def _state_dir() -> Path:
    return ava_home() / "state" / "im_bridge"


# Direct-DB reads (R3 door ④, decision 2): the notice bridge reads
# agent_notices itself instead of polling the gateway's HTTP endpoints, so
# notice delivery is decoupled from gateway availability — a paused cluster
# (migration window) can no longer stall pushes, and the post-pause flood of
# backlogged notices never happens. Row shape mirrors the gateway's
# NoticeItem exactly, so the push/filter logic is unchanged.
_SELECT_NOTICE = (
    "SELECT n.id, n.agent_id, t.label, n.title, n.content, n.priority, "
    "n.require_response, n.blocking, n.created_at, n.resolved_at, n.resolution, n.reply, n.task_id "
    "FROM agent_notices n JOIN agents t ON t.id = n.agent_id "
)


def _row_to_notice(r: tuple[Any, ...]) -> dict[str, Any]:
    """One agent_notices row as the dict the push/filter logic consumes
    (same keys as the gateway's NoticeItem wire shape)."""
    return {
        "id": int(r[0]),
        "agent_id": int(r[1]),
        "agent_label": r[2],
        "title": r[3],
        "content": r[4],
        "priority": r[5],
        "require_response": bool(r[6]),
        "blocking": bool(r[7]),
        "created_at": r[8],
        "resolved_at": r[9],
        "resolution": r[10],
        "reply": r[11],
        "task_id": r[12],
    }


def _parse_filter_tokens(rest: list[str]) -> dict[str, Any]:
    """`{"min_priority", "agent"}` from the tokens after `/notice filter`."""
    filters: dict[str, Any] = {"min_priority": None, "agent": None}
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok.upper() in _PRIORITY_RANK or tok.lower() == "off":
            filters["min_priority"] = None if tok.lower() == "off" else tok.upper()
        elif tok.lower() == "agent" and i + 1 < len(rest):
            filters["agent"] = int(rest[i + 1])
            i += 1
        i += 1
    return filters


class NoticeBridge:
    """Push fleet notices to the owner chat; own reply mode + filters."""

    def __init__(self, core: Any, config: ImBridgeConfig, *, db_pool: Any = None) -> None:
        self._config = config
        self.core = core
        self.db_pool = db_pool
        self.poll_store = NoticePollStore(core.outbound_store)
        self._cursor = 0
        self._legacy_cursor: int | None = None
        self._cutover_warned = False
        self._reply_modes: dict[str, dict[str, Any]] = {}  # chat_id -> mode
        self._filters: dict[str, Any] = {"min_priority": None, "agent": None}
        self._load_state()

    # -- persistence -------------------------------------------------------

    def _load_state(self) -> None:
        d = _state_dir()
        d.mkdir(parents=True, exist_ok=True)
        with suppress(FileNotFoundError, json.JSONDecodeError):
            value = json.loads((d / "notice_cursor.json").read_text())
            if type(value) is int and value >= 0:
                self._legacy_cursor = self._cursor = value
        with suppress(FileNotFoundError, json.JSONDecodeError):
            self._filters = json.loads((d / "notice_filters.json").read_text())
        if not isinstance(self._filters, dict):
            self._filters = {"min_priority": None, "agent": None}

    def _save(self, name: str, value: Any) -> None:
        try:
            (_state_dir() / name).write_text(json.dumps(value))
        except OSError:
            _log.warning("notice state save failed: %s", name)

    # -- gateway -----------------------------------------------------------

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        g = self.core.gateway
        resp = await (await g._http()).get(path, params=params, headers=g._headers())
        resp.raise_for_status()
        return resp.json()

    async def _post(self, path: str, body: dict[str, Any]) -> None:
        g = self.core.gateway
        resp = await (await g._http()).post(path, json=body, headers=g._headers())
        resp.raise_for_status()

    # -- polling -----------------------------------------------------------

    def initialize_poll(self) -> None:
        """Establish native cutover before daemon readiness and first poll."""
        _, diagnostic, reason = self.poll_store.initialize_notice_poll(self._legacy_cursor)
        self._cursor = max(self._cursor, diagnostic)
        if reason == NoticePollImportReason.LEGACY_HISTORY_UNKNOWN and not self._cutover_warned:
            self._cutover_warned = True
            _log.warning(
                "normal notice poll retains unknown legacy history; explicit listing remains available"
            )

    async def poll_once(self) -> None:
        """Accept normal notice decisions; the shared worker owns provider calls."""
        try:
            await asyncio.to_thread(self.initialize_poll)
            notices = await asyncio.to_thread(self._notices_after, 0)
        except Exception as exc:
            _log.warning("notice acceptance read held class=%s", type(exc).__name__)
            return
        for notice in notices:
            if not await self._accept_normal_notice(notice):
                self._warn_push_held(notice)
                break
            self._cursor = max(self._cursor, int(notice["id"]))
            self._save("notice_cursor.json", self._cursor)

    async def _accept_normal_notice(self, notice: dict[str, Any]) -> bool:
        filtered = not self._passes_filter(notice)
        snapshot = {
            key: notice[key]
            for key in (
                "id",
                "agent_id",
                "title",
                "content",
                "priority",
                "require_response",
                "agent_label",
            )
        }
        snapshot["filters"] = dict(self._filters)
        intent = None
        if not filtered:
            adapter = self.core.adapters.get("telegram")
            if adapter is None:
                return False
            text, buttons = self._render_notice(notice)
            try:
                recipient, prepared = await adapter.prepare_notice_owner(text, tuple(buttons))
            except Exception as exc:
                _log.warning(
                    "notice target unavailable notice=%s class=%s", notice["id"], type(exc).__name__
                )
                return False
            intent = OutboundIntent(
                channel="telegram",
                chat_id=recipient,
                agent_id=int(notice["agent_id"]),
                source=OutboundSource(
                    kind=OutboundSourceKind.NOTICE, identity=str(notice["id"]), block_idx=0
                ),
                prepared=prepared,
            )
        try:
            await asyncio.to_thread(
                self.poll_store.accept_notice,
                int(notice["id"]),
                snapshot,
                intent,
                filtered=filtered,
            )
        except Exception as exc:
            _log.warning(
                "notice acceptance held notice=%s class=%s", notice["id"], type(exc).__name__
            )
            return False
        return True

    def _notices_after(self, after: int, limit: int | None = None) -> list[dict[str, Any]]:
        """Unaccepted open notices above the immutable cutover, regardless of high ID."""
        from base.db import NOTICE_FYI_TTL_DAYS

        del after  # Diagnostic compatibility argument, never an eligibility watermark.
        if limit is None:
            limit = self._config.notices_open_default_limit
        with self.db_pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                _SELECT_NOTICE + "JOIN im_bridge_notice_poll_state s ON s.singleton "
                "LEFT JOIN im_bridge_notice_acceptances a ON a.notice_id=n.id "
                "WHERE n.id > s.legacy_floor AND a.notice_id IS NULL "
                "AND n.resolved_at IS NULL AND (n.require_response OR "
                "n.created_at > now() - make_interval(days => %s)) ORDER BY n.id ASC LIMIT %s",
                (NOTICE_FYI_TTL_DAYS, limit),
            )
            return [_row_to_notice(r) for r in cur.fetchall()]

    def _open_notices(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Every open notice, both kinds, priority-then-newest — the
        direct-DB twin of the gateway's /api/notices/open query. Without an
        explicit limit the listing caps at services.im_bridge_notice_open_limit;
        callers may narrow it."""
        from base.db import NOTICE_FYI_TTL_DAYS

        if limit is None:
            limit = self._config.im_bridge_notice_open_limit

        with write_transaction(self.db_pool) as conn, conn.cursor() as cur:
            # Lazy FYI expiry, same as the gateway's open feed: expired FYIs
            # are auto-resolved so the queue view and every later query agree.
            cur.execute(
                "UPDATE agent_notices SET resolved_at = now(), resolution = 'read' "
                "WHERE NOT require_response AND resolved_at IS NULL "
                "AND created_at <= now() - make_interval(days => %s)",
                (NOTICE_FYI_TTL_DAYS,),
            )
            cur.execute(
                _SELECT_NOTICE + "WHERE n.resolved_at IS NULL AND (n.require_response OR "
                "n.created_at > now() - make_interval(days => %s)) "
                + "ORDER BY n.priority ASC, n.created_at DESC, n.id DESC LIMIT %s",
                (NOTICE_FYI_TTL_DAYS, limit),
            )
            return [_row_to_notice(r) for r in cur.fetchall()]

    def _passes_filter(self, n: dict[str, Any]) -> bool:
        f = self._filters or {}
        agent = f.get("agent")
        if agent is not None and int(n["agent_id"]) != int(agent):
            return False
        min_prio = f.get("min_priority")
        if not min_prio or n["require_response"]:
            return True  # decisions always push
        return _PRIORITY_RANK.get(n["priority"], 9) <= _PRIORITY_RANK.get(min_prio, 9)

    @staticmethod
    def _render_notice(
        n: dict[str, Any], prefix: str | None = None
    ) -> tuple[str, list[tuple[str, str]]]:
        lines = [f"🔔 {n['title']}"]  # emoji-ok: Telegram notification icon (user-facing)
        if prefix:
            lines.append(prefix)
        if n.get("content"):
            lines.append(str(n["content"]))
        label = n.get("agent_label") or ""
        lines.append(f"[#{n['agent_id']} {label}] · {n['priority']}".rstrip())
        cb = f"{n['agent_id']}:{n['id']}"
        buttons: list[tuple[str, str]] = []
        if n["require_response"]:
            buttons = [
                ("✏️ Reply", f"{_CB_REPLY}{cb}"),  # emoji-ok: button
                ("✖️ Dismiss", f"{_CB_DISMISS}{cb}"),  # emoji-ok: button
            ]
        else:
            # FYI is answerable too (Task #1061): the reply rides the same
            # reply-mode window and resolve action=answer as a require_response
            # notice, so the text reaches the notice's agent either way.
            buttons = [
                ("✏️ Reply", f"{_CB_REPLY}{cb}"),  # emoji-ok: button
                ("✓ Got it", f"{_CB_READ}{cb}"),  # emoji-ok: button
            ]
        buttons.append(("📋 Queue", _CB_LIST))  # emoji-ok: queue-view button
        return "\n\n".join(lines), buttons

    async def _push(self, n: dict[str, Any], *, prefix: str | None = None) -> bool:
        """Deliver one notice; return True when it was sent."""
        adapter = self.core.adapters.get("telegram")
        if adapter is None:
            return False
        text, buttons = self._render_notice(n, prefix)
        try:
            await adapter.send_to_owner(text, buttons=buttons)
            return True
        except Exception:
            _log.warning("notice push failed (notice %s)", n["id"], exc_info=True)
            return False

    #: Cooldown for the "push held" warning — one line per stall, not one per
    #: 3s poll round.
    _push_held_warned_at = 0.0

    def _warn_push_held(self, n: dict[str, Any]) -> None:
        """Log that the cursor is held on notice ``n`` — throttled, because
        an absent telegram adapter would otherwise log every poll round."""
        now = time.monotonic()
        if now - self._push_held_warned_at < 300:
            return
        self._push_held_warned_at = now
        _log.warning(
            "notice %s acceptance held or response lost; retained receipt governs the next poll",
            n["id"],
        )

    # -- callbacks ---------------------------------------------------------

    async def handle_callback(self, chat_id: str, data: str) -> str | None:
        """Consume a notice: prefixed button tap; return the hint text to
        show the user (None when the tap is not ours)."""
        if data == _CB_LIST:
            return await self.list_queue()
        for prefix, action in ((_CB_REPLY, "reply"), (_CB_READ, "read"), (_CB_DISMISS, "dismiss")):
            if data.startswith(prefix):
                agent_id, notice_id = data[len(prefix) :].split(":", 1)
                if action == "reply":
                    return self._arm_reply_mode(chat_id, agent_id, notice_id)
                return await self._resolve(action, agent_id, notice_id)
        return None

    def _arm_reply_mode(self, chat_id: str, agent_id: str, notice_id: str) -> str:
        """Tap [Reply]: arm the reply window on this chat. A new tap replaces
        an older mode; expired modes are dropped first."""
        window_seconds = self._config.im_bridge_notice_reply_window_seconds
        self._reply_modes = {
            k: v for k, v in self._reply_modes.items() if time.time() <= v["expires_at"]
        }
        self._reply_modes[str(chat_id)] = {
            "notice_id": int(notice_id),
            "agent_id": int(agent_id),
            "expires_at": time.time() + window_seconds,
        }
        return (
            f"✏️ Reply mode: messages you send in the next {window_seconds // 60} min go to"  # emoji-ok: Telegram reply-mode hint (user-facing)
            "that notice as replies (/cancel to exit)"
        )

    async def _resolve(self, action: str, agent_id: str, notice_id: str) -> str:
        """Resolve read/dismiss via the gateway; returns a hint."""
        await self._post(
            f"/api/agents/{agent_id}/notices/{notice_id}/resolve",
            {"action": action, "reply": None},
        )
        return "✅ Handled" if action == "dismiss" else "✓ Marked as read"  # emoji-ok: hint

    async def list_queue(self) -> str | None:
        """List every open notice (both kinds) as its own message with
        processing buttons — the queue view (Task #941). Returns a summary
        hint; each item is pushed like a fresh notice."""
        try:
            if self.db_pool is not None:
                notices = await asyncio.to_thread(self._open_notices)
            else:
                notices = await self._get(
                    "/api/notices/open",
                    {
                        "include_awaiting": True,
                        "limit": self._config.im_bridge_notice_open_limit,
                    },
                )
        except Exception:
            _log.warning("notice list failed", exc_info=True)
            return "Queue lookup failed, try again later"
        if not notices:
            return "🎉 No notices queued"  # emoji-ok: Telegram queue-empty hint
        for i, n in enumerate(notices, start=1):
            await self._push(
                n,
                prefix=f"📋 Queue {i}/{len(notices)}",  # emoji-ok: Telegram queue label
            )  # emoji-ok: Telegram queue label
        return f"📋 Queue: {len(notices)} notices open, listed below"  # emoji-ok: Telegram queue summary

    async def handle_inbound(self, chat_id: str, text: str) -> str | None:
        """Reply-mode window: plain text resolves the notice; returns a
        confirmation text when consumed, else None (normal flow keeps it)."""
        mode = self._reply_modes.get(str(chat_id))
        if mode is None:
            return None
        if time.time() > mode["expires_at"]:
            self._reply_modes.pop(str(chat_id), None)
            return None
        if text.startswith("/cancel"):
            self._reply_modes.pop(str(chat_id), None)
            return "Left reply mode"
        if text.startswith("/"):
            return None  # other commands pass through to the normal flow
        try:
            await self._post(
                f"/api/agents/{mode['agent_id']}/notices/{mode['notice_id']}/resolve",
                {"action": "answer", "reply": text},
            )
        except Exception:
            # The notice is probably already resolved (409) or the gateway was
            # briefly unreachable. Pop the mode either way: a stuck reply mode
            # swallows every following user message as another reply attempt
            # and looks like the push stream died (Task #1069 user report).
            _log.warning(
                "notice reply resolve failed notice=%s agent=%s chat=%s — dropping reply mode",
                mode["notice_id"],
                mode["agent_id"],
                chat_id,
                exc_info=True,
            )
            self._reply_modes.pop(str(chat_id), None)
            return copy.REPLY_RESOLVE_FAILED
        self._reply_modes.pop(str(chat_id), None)
        # The reply lands in the notice's agent — if that is not the agent
        # this chat switched to, its follow-up reply will NOT be pushed here
        # (the subscription only covers the switched agent). The reply itself
        # succeeded, so confirm it like any notice reply and add a neutral
        # hint naming the agent the next message will go to (user ruling
        # 2026-08-30).
        switched = self._switched_agent(chat_id)
        if switched is not None and switched != mode["agent_id"]:
            return copy.REPLY_SENT_OTHER_AGENT.format(agent_id=mode["agent_id"], switched=switched)
        return copy.REPLY_SENT

    def _switched_agent(self, chat_id: str) -> int | None:
        """The agent this chat is currently switched to, if any."""

        for key, agent_id in self.core._switch_state.items():
            if key.endswith(f":{chat_id}"):
                return int(agent_id)
        return None

    # -- /notice command ---------------------------------------------------

    def cmd_notice(self, args: str) -> str:
        """/notice [filter ...] — show status or update the push filter."""
        parts = args.strip().split()
        if not parts or parts[0] != "filter":
            f = self._filters or {}
            bits = [
                f"min_priority={f.get('min_priority') or 'off'}",
                f"agent={f.get('agent') or 'all'}",
            ]
            return (
                "Notice push: "
                + " ".join(bits)
                + "  (usage: /notice filter <P0|P1|P2|off> [agent <id>])"
            )
        rest = parts[1:]
        if not rest:
            f = self._filters or {}
            return (
                "Current filter: " + " ".join(f"{k}={v}" for k, v in f.items() if v is not None)
                or "none (all pushed)"
            )
        self._filters = _parse_filter_tokens(rest)
        self._save("notice_filters.json", self._filters)
        return "Filter updated: " + json.dumps(self._filters)
