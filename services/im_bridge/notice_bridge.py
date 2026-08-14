"""Telegram notice bridge (Task #884).

Polls the gateway's /api/notices/live for new fleet notices (an agent's
``ava.ui.notify`` -> agent_notices rows) and pushes each to the Telegram owner
chat — a channel independent of the /switch conversation flow. Pushed notices
carry inline buttons:

- [Reply] (both kinds, Task #1061 made FYI answerable too) arms a 5-minute
  reply mode: plain-text messages in that window resolve the notice with that
  text as the answer, so chatting with agents and answering notices never
  collide; /cancel exits early.
- [OK] / [Close] resolve an FYI as read or dismiss a response-required notice.

A ``/notice filter`` command restricts what gets pushed (minimum priority
and/or a single agent). Filter + the poll cursor persist under
``$AVA_HOME/state/im_bridge/`` so a daemon restart resumes without replaying
resolved notices.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from services.im_bridge import copy
from shared.config import settings

_log = logging.getLogger("services.im_bridge.notice_bridge")

REPLY_WINDOW_SECONDS = 300  # reply mode window after tapping [Reply]

_CB_REPLY = "notice:reply:"
_CB_READ = "notice:read:"
_CB_DISMISS = "notice:dismiss:"
_CB_LIST = "notice:list"

_PRIORITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


def _state_dir() -> Path:
    return Path(settings.general.ava_home) / "state" / "im_bridge"


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


class NoticeBridge:
    """Push fleet notices to the owner chat; own reply mode + filters."""

    def __init__(self, core: Any, *, db_pool: Any = None) -> None:
        self.core = core
        self.db_pool = db_pool
        self._cursor = 0
        self._reply_modes: dict[str, dict[str, Any]] = {}  # chat_id -> mode
        self._filters: dict[str, Any] = {"min_priority": None, "agent": None}
        self._load_state()

    # -- persistence -------------------------------------------------------

    def _load_state(self) -> None:
        d = _state_dir()
        d.mkdir(parents=True, exist_ok=True)
        for name, attr in (("notice_cursor.json", "_cursor"), ("notice_filters.json", "_filters")):
            with suppress(FileNotFoundError, json.JSONDecodeError):
                setattr(self, attr, json.loads((d / name).read_text()))
        if not isinstance(self._filters, dict):
            self._filters = {"min_priority": None, "agent": None}
        if not isinstance(self._cursor, int):
            # A corrupted cursor (list/dict/str) would make every _notices_after
            # SQL parameter wrong — a permanent 3s error loop instead of a
            # recoverable state (audit round 2, P2).
            self._cursor = 0

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

    async def poll_once(self) -> None:
        """Fetch notices newer than the cursor — directly from agent_notices
        (R3 door ④) — push the ones that pass the filter, advance the cursor.

        Same semantics as the old /api/notices/live poll: open notices only
        (resolved rows stop being returned), FYI expiry honored (an expired FYI
        is excluded once the open feed auto-resolved it — the cursor must not
        stall on it)."""
        try:
            if self.db_pool is not None:
                notices = await asyncio.to_thread(self._notices_after, self._cursor)
            else:
                notices = await self._get("/api/notices/live", {"after": self._cursor})
        except Exception:
            _log.warning("notice poll failed", exc_info=True)
            return
        if not notices:
            return
        # Advance the cursor only past notices that were delivered (or
        # deliberately filtered out). The old code moved it unconditionally,
        # so a failed push (Telegram send error, adapter absent) silently
        # dropped the notice forever — at-most-once (audit round 2, P1).
        # Holding the cursor retries the notice on the next round instead.
        last_delivered = self._cursor
        for n in notices:
            if self._passes_filter(n) and not await self._push(n):
                self._warn_push_held(n)
                break
            last_delivered = int(n["id"])
        if last_delivered > self._cursor:
            self._cursor = last_delivered
            self._save("notice_cursor.json", self._cursor)

    def _notices_after(self, after: int, limit: int = 200) -> list[dict[str, Any]]:
        """Open notices with id > after, both kinds, oldest-first — the
        direct-DB twin of the gateway's /api/notices/live query."""
        from shared.db import NOTICE_FYI_TTL_DAYS

        with self.db_pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                _SELECT_NOTICE
                + "WHERE n.id > %s AND n.resolved_at IS NULL AND (n.require_response OR "
                "n.created_at > now() - make_interval(days => %s)) " + "ORDER BY n.id ASC LIMIT %s",
                (after, NOTICE_FYI_TTL_DAYS, limit),
            )
            return [_row_to_notice(r) for r in cur.fetchall()]

    def _open_notices(self, limit: int = 50) -> list[dict[str, Any]]:
        """Every open notice, both kinds, priority-then-newest — the
        direct-DB twin of the gateway's /api/notices/open query."""
        from shared.db import NOTICE_FYI_TTL_DAYS

        with self.db_pool.connection() as conn, conn.cursor() as cur:
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

    async def _push(self, n: dict[str, Any], *, prefix: str | None = None) -> bool:
        """Deliver one notice; return True when it was sent."""
        adapter = self.core.adapters.get("telegram")
        if adapter is None:
            return False
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
        try:
            await adapter.send_to_owner("\n\n".join(lines), buttons=buttons)
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
            "notice %s not pushed (adapter missing or send failed) — cursor held, retrying next round",
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
        self._reply_modes = {
            k: v for k, v in self._reply_modes.items() if time.time() <= v["expires_at"]
        }
        self._reply_modes[str(chat_id)] = {
            "notice_id": int(notice_id),
            "agent_id": int(agent_id),
            "expires_at": time.time() + REPLY_WINDOW_SECONDS,
        }
        return (
            f"✏️ Reply mode: messages you send in the next {REPLY_WINDOW_SECONDS // 60} min go to"  # emoji-ok: Telegram reply-mode hint (user-facing)
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
                notices = await asyncio.to_thread(self._open_notices, 50)
            else:
                notices = await self._get(
                    "/api/notices/open", {"include_awaiting": True, "limit": 50}
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
        # (the subscription only covers the switched agent). Tell the user
        # instead of letting them wait for a reply that cannot arrive.
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
        self._filters = {"min_priority": None, "agent": None}
        i = 0
        while i < len(rest):
            tok = rest[i]
            if tok.upper() in _PRIORITY_RANK or tok.lower() == "off":
                self._filters["min_priority"] = None if tok.lower() == "off" else tok.upper()
            elif tok.lower() == "agent" and i + 1 < len(rest):
                self._filters["agent"] = int(rest[i + 1])
                i += 1
            i += 1
        self._save("notice_filters.json", self._filters)
        return "Filter updated: " + json.dumps(self._filters)
