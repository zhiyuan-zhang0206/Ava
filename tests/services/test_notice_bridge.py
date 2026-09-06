"""`services.im_bridge.notice_bridge` — fleet notices → Telegram owner chat
(Task #884): polling cursor, filter, reply window, /notice command.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import psycopg
import pytest

from services.im_bridge.core import IMBridgeCore
from services.im_bridge.notice_bridge import NoticeBridge, _state_dir
from shared.config import settings


class FakeGateway:
    """Minimal gateway stand-in: live poll / open feed / resolve recorded."""

    def _headers(self) -> dict[str, str]:
        return {}

    async def _http(self) -> Any:
        return self

    async def get(self, path: str, params: dict[str, Any] | None = None, **_: Any) -> Any:
        if path == "/api/notices/live":
            after = int((params or {}).get("after", 0))
            return _Resp([n for n in self.notices if n["id"] > after])
        if path == "/api/notices/open":
            return _Resp(list(self.open_notices))
        return _Resp([])

    def __init__(self, *, resolve_failures: int = 0) -> None:
        self.notices: list[dict[str, Any]] = []
        self.open_notices: list[dict[str, Any]] = []
        self.resolved: list[tuple[int, int, str, str | None]] = []
        self.resolve_failures = resolve_failures
        self._client = None

    async def post(self, path: str, json: dict[str, Any] | None = None, **_: Any) -> Any:
        if self.resolve_failures > 0:
            self.resolve_failures -= 1
            raise RuntimeError("resolve failed (notice already handled)")
        parts = path.split("/")
        agent_id = int(parts[3])
        notice_id = int(parts[5])
        body = json or {}
        self.resolved.append((agent_id, notice_id, body["action"], body.get("reply")))
        return _Resp(None)


class _Resp:
    def __init__(self, data: Any) -> None:
        self._data = data

    def raise_for_status(self) -> None:
        pass

    def json(self) -> Any:
        return self._data


class FakeAdapter:
    channel = "telegram"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, list | None]] = []

    async def send_to_owner(
        self,
        text: str,
        *,
        markdown: bool = False,
        buttons: list | None = None,
    ) -> None:
        del markdown
        self.sent.append((text, "owner", buttons))  # pyright: ignore[reportUnknownMemberType]


def _notice(
    nid: int, title: str, *, agent: int = 1, priority: str = "P2", require_response: bool = False
) -> dict[str, Any]:
    return {
        "id": nid,
        "agent_id": agent,
        "agent_label": "\u6d4b\u8bd5",
        "title": title,
        "content": "body",
        "priority": priority,
        "require_response": require_response,
        "blocking": False,
    }


def _bridge(
    tmp_path: Any, gateway: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> tuple[NoticeBridge, FakeAdapter]:
    monkeypatch.setattr(settings.general, "ava_home", tmp_path)
    core = IMBridgeCore()
    core.gateway = gateway  # type: ignore[assignment]
    adapter = FakeAdapter()
    core.adapters["telegram"] = adapter  # type: ignore[assignment]
    bridge = NoticeBridge(core)
    return bridge, adapter


def test_poll_pushes_new_notices_and_advances_cursor(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = FakeGateway()
    gateway.notices = [_notice(1, "A"), _notice(2, "B")]
    bridge, adapter = _bridge(tmp_path, gateway, monkeypatch)

    asyncio.run(bridge.poll_once())
    assert len(adapter.sent) == 2  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    assert "A" in adapter.sent[0][0]  # pyright: ignore[reportUnknownMemberType]
    assert bridge._cursor == 2
    # with min_priority=P1: P3 FYI filtered, require_response always pushes
    bridge._filters = {"min_priority": "P1", "agent": None}
    gateway.notices = [
        _notice(3, "low", priority="P3"),
        _notice(4, "decide", require_response=True),
    ]
    asyncio.run(bridge.poll_once())
    assert (
        len(adapter.sent) == 3  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    )  # low filtered, decide pushed
    assert bridge._cursor == 4


def test_filter_agent_restricts_pushes(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = FakeGateway()
    gateway.notices = [_notice(1, "from 2", agent=2), _notice(2, "from 1", agent=1)]
    bridge, adapter = _bridge(tmp_path, gateway, monkeypatch)
    bridge._filters = {"min_priority": None, "agent": 2}

    import asyncio

    asyncio.run(bridge.poll_once())
    assert len(adapter.sent) == 1  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    assert "from 2" in adapter.sent[0][0]  # pyright: ignore[reportUnknownMemberType]


def test_reply_mode_window_resolves_notice(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = FakeGateway()
    bridge, _ = _bridge(tmp_path, gateway, monkeypatch)

    import asyncio

    # arm reply mode via callback
    hint = asyncio.run(bridge.handle_callback("12345", "notice:reply:7:42"))
    assert hint is not None and "Reply mode" in hint
    assert "12345" in bridge._reply_modes

    # a plain text inside the window resolves with that text
    out = asyncio.run(bridge.handle_inbound("12345", "\u6211\u540c\u610f"))
    assert out is not None
    assert gateway.resolved == [(7, 42, "answer", "\u6211\u540c\u610f")]
    assert "12345" not in bridge._reply_modes


def test_reply_mode_cancel_and_expiry(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = FakeGateway()
    bridge, _ = _bridge(tmp_path, gateway, monkeypatch)

    import asyncio

    asyncio.run(bridge.handle_callback("1", "notice:reply:7:42"))
    out = asyncio.run(bridge.handle_inbound("1", "/cancel"))
    assert out is not None
    assert gateway.resolved == []

    # expired mode: dropped, text flows through
    asyncio.run(bridge.handle_callback("1", "notice:reply:7:42"))
    bridge._reply_modes["1"]["expires_at"] = 0
    out = asyncio.run(bridge.handle_inbound("1", "hi"))
    assert out is None
    assert gateway.resolved == []


def test_fyi_reply_mode_resolves_answer(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Task #1061: an FYI notice's [Reply] button arms the same reply mode as a
    require_response notice; the text resolves it as an answer, so the reply
    reaches the notice's agent through the unified channel."""
    gateway = FakeGateway()
    bridge, _ = _bridge(tmp_path, gateway, monkeypatch)

    import asyncio

    # FYI notice pushed with a reply button (require_response False)
    gateway.notices = [_notice(1, "FYI done", require_response=False)]
    asyncio.run(bridge.poll_once())

    # arm reply mode on the FYI notice and answer
    hint = asyncio.run(bridge.handle_callback("12345", "notice:reply:1:1"))
    assert hint is not None and "Reply mode" in hint
    out = asyncio.run(bridge.handle_inbound("12345", "\u6536\u5230\uff0c\u8c22\u8c22"))
    assert out is not None
    assert gateway.resolved == [(1, 1, "answer", "\u6536\u5230\uff0c\u8c22\u8c22")]


def test_reply_mode_drops_on_resolve_failure(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (Task #1069 user report): a failed resolve (notice already
    handled / gateway hiccup) must NOT leave the reply mode armed — a stuck
    mode swallows every following user message as another reply attempt and
    looks like the push stream died. The mode pops and the user gets a hint."""
    gateway = FakeGateway(resolve_failures=1)
    bridge, _ = _bridge(tmp_path, gateway, monkeypatch)

    import asyncio

    asyncio.run(bridge.handle_callback("12345", "notice:reply:7:42"))
    out = asyncio.run(bridge.handle_inbound("12345", "\u6211\u540c\u610f"))
    assert out is not None and "Reply failed to send" in out
    # mode popped: the next message flows through to the normal chat path
    assert "12345" not in bridge._reply_modes
    assert gateway.resolved == []


def test_reply_hint_names_next_agent_when_notice_agent_is_not_switched(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task #2162 (user ruling 2026-08-30): replying to another agent's
    notice succeeds — it is a normal confirmation, not a warning. The reply
    confirmation matches the same-agent one and appends a neutral hint naming
    the agent the next message will be sent to."""
    from services.im_bridge import copy

    gateway = FakeGateway()
    bridge, _ = _bridge(tmp_path, gateway, monkeypatch)
    bridge.core._switch_state["telegram:12345"] = 405

    import asyncio

    asyncio.run(bridge.handle_callback("12345", "notice:reply:7:42"))
    out = asyncio.run(bridge.handle_inbound("12345", "\u56de\u590d\u5185\u5bb9"))
    assert gateway.resolved == [(7, 42, "answer", "\u56de\u590d\u5185\u5bb9")]
    response = copy.REPLY_SENT_OTHER_AGENT.format(agent_id=7, switched=405)
    assert out == response
    # same success presentation as the same-agent reply, plus the routing hint
    assert response.startswith(copy.REPLY_SENT)
    assert "Your next message will be sent to agent" in response
    assert response.endswith("agent #405.")  # next message routes to the switched agent

    # same-agent notice: plain confirmation, no routing hint
    asyncio.run(bridge.handle_callback("12345", "notice:reply:405:43"))
    out2 = asyncio.run(bridge.handle_inbound("12345", "hi"))
    assert out2 == copy.REPLY_SENT


def test_read_dismiss_callbacks(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = FakeGateway()
    bridge, _ = _bridge(tmp_path, gateway, monkeypatch)

    import asyncio

    out = asyncio.run(bridge.handle_callback("1", "notice:read:7:42"))
    assert out == "✓ Marked as read"
    out2 = asyncio.run(bridge.handle_callback("1", "notice:dismiss:8:43"))
    assert out2 == "✅ Handled"  # emoji-ok: Telegram hint text (user-facing)
    assert gateway.resolved == [(7, 42, "read", None), (8, 43, "dismiss", None)]


def test_list_queue_pushes_each_open_notice(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The queue view pushes every open notice (both kinds) with processing
    buttons and returns a summary (Task #941)."""
    gateway = FakeGateway()
    gateway.open_notices = [
        _notice(1, "A", require_response=True),
        _notice(2, "B"),
    ]
    bridge, adapter = _bridge(tmp_path, gateway, monkeypatch)

    import asyncio

    hint = asyncio.run(bridge.list_queue())
    assert hint is not None and "2 notices open" in hint
    assert len(adapter.sent) == 2  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    assert "Queue 1/2" in adapter.sent[0][0]  # pyright: ignore[reportUnknownMemberType]
    # each row carries an action button
    b0 = adapter.sent[0][2]  # pyright: ignore[reportUnknownMemberType]
    b1 = adapter.sent[1][2]  # pyright: ignore[reportUnknownMemberType]
    assert b0 is not None and b0[0][0] == "✏️ Reply"  # emoji-ok: button label
    assert (
        b1 is not None and b1[0][0] == "✏️ Reply"  # emoji-ok: button label
    )  # emoji-ok: button label  # FYI answerable too (Task #1061)
    assert b1 is not None and b1[1][0] == "✓ Got it"
    # queue button
    assert b0 is not None and b0[-1][1] == "notice:list"


def test_list_queue_empty(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = FakeGateway()
    gateway.open_notices = []
    bridge, adapter = _bridge(tmp_path, gateway, monkeypatch)

    import asyncio

    hint = asyncio.run(bridge.list_queue())
    assert hint is not None and "No notices queued" in hint
    assert adapter.sent == []  # pyright: ignore[reportUnknownMemberType]


def test_list_queue_callback(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tapping [📋 Queue] (notice:list) runs the queue view."""  # emoji-ok: button label
    gateway = FakeGateway()
    gateway.open_notices = [_notice(1, "A")]
    bridge, adapter = _bridge(tmp_path, gateway, monkeypatch)

    import asyncio

    hint = asyncio.run(bridge.handle_callback("1", "notice:list"))
    assert hint is not None
    assert len(adapter.sent) == 1  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]


def test_cmd_notice_filter_updates_and_persists(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = FakeGateway()
    bridge, _ = _bridge(tmp_path, gateway, monkeypatch)

    out = bridge.cmd_notice("filter P1 agent 5")
    assert '"min_priority": "P1"' in out
    assert '"agent": 5' in out
    assert (_state_dir() / "notice_filters.json").exists()
    # restart: filter survives
    bridge2 = NoticeBridge(_bridge(tmp_path, gateway, monkeypatch)[0].core)
    assert bridge2._filters["min_priority"] == "P1"
    assert bridge2._filters["agent"] == 5


# ── direct-DB reads (R3 door ④, decision 2) ───────────────────────────────
# The notice bridge reads agent_notices itself (decoupled from gateway
# availability); these tests exercise the real DB path with a real pool.


def _direct_bridge(db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from psycopg_pool import ConnectionPool

    from shared.config import settings

    pool = ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=2, open=True)
    monkeypatch.setattr(settings.general, "ava_home", tmp_path)
    core = IMBridgeCore(db_pool=pool)
    adapter = FakeAdapter()
    core.adapters["telegram"] = adapter  # type: ignore[assignment]
    bridge = NoticeBridge(core, db_pool=pool)
    return bridge, adapter, pool


def _seed_notice(
    db_conn: psycopg.Connection, agent_id: int, title: str, *, prio: str = "P2"
) -> int:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_notices (agent_id, local_id, title, priority, require_response, blocking) "
            "VALUES (%s, COALESCE((SELECT MAX(local_id) FROM agent_notices WHERE agent_id = %s), -1) + 1, %s, %s, false, false) "
            "RETURNING id",
            (agent_id, agent_id, title, prio),
        )
        row = cur.fetchone()
        assert row is not None
        nid = row[0]
    db_conn.commit()
    return int(nid)


def test_poll_reads_directly_from_db(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, db_conn: psycopg.Connection
) -> None:
    import asyncio

    from tests.conftest import spawn_agent

    agent_id = spawn_agent()
    nid = _seed_notice(db_conn, agent_id, "direct db notice")
    bridge, adapter, pool = _direct_bridge(db_conn, tmp_path, monkeypatch)
    try:
        asyncio.run(bridge.poll_once())
        assert len(adapter.sent) == 1  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        assert "direct db notice" in adapter.sent[0][0]  # pyright: ignore[reportUnknownMemberType]
        assert bridge._cursor == nid, "cursor advanced past the new notice"

        # Resolved notices stop being returned (cursor stays put).
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agent_notices SET resolved_at = now(), resolution = 'read' WHERE id = %s",
                (nid,),
            )
        db_conn.commit()
        asyncio.run(bridge.poll_once())
        assert len(adapter.sent) == 1, "no re-delivery of resolved notices"  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    finally:
        pool.close()


def test_list_queue_reads_directly_from_db(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, db_conn: psycopg.Connection
) -> None:
    import asyncio

    from tests.conftest import spawn_agent

    agent_id = spawn_agent()
    _seed_notice(db_conn, agent_id, "queue item")
    bridge, adapter, pool = _direct_bridge(db_conn, tmp_path, monkeypatch)
    try:
        hint = asyncio.run(bridge.list_queue())
        assert hint is not None and "1 notices open" in hint
        assert any("queue item" in s[0] for s in adapter.sent)  # pyright: ignore[reportUnknownMemberType]
    finally:
        pool.close()


class _FlakyAdapter(FakeAdapter):
    """Fails the first send_to_owner call, succeeds after."""

    def __init__(self, fail_first: int = 1) -> None:
        super().__init__()
        self._failures_left = fail_first

    async def send_to_owner(
        self,
        text: str,
        *,
        markdown: bool = False,
        buttons: list | None = None,
    ) -> None:
        if self._failures_left > 0:
            self._failures_left -= 1
            raise RuntimeError("telegram API down")
        await super().send_to_owner(text, markdown=markdown, buttons=buttons)  # pyright: ignore[reportUnknownMemberType]


def test_poll_holds_cursor_when_push_fails(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression (audit round 2, P1): a failed push used to advance the
    cursor anyway, silently dropping the notice forever. The cursor must
    hold at the failed notice so the next round retries it, and notices
    behind it stay queued."""
    gateway = FakeGateway()
    gateway.notices = [_notice(1, "A"), _notice(2, "B"), _notice(3, "C")]
    bridge, _ = _bridge(tmp_path, gateway, monkeypatch)
    flaky = _FlakyAdapter(fail_first=1)  # first send fails, then succeeds
    bridge.core.adapters["telegram"] = flaky  # type: ignore[assignment]

    asyncio.run(bridge.poll_once())
    assert bridge._cursor == 0, "cursor must not advance past a failed push"
    assert (
        flaky.sent == []  # pyright: ignore[reportUnknownMemberType]
    )  # notice 1 failed; 2 and 3 not attempted yet

    # next round: 1 succeeds, 2 and 3 pushed, cursor reaches 3
    asyncio.run(bridge.poll_once())
    assert len(flaky.sent) == 3  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    assert bridge._cursor == 3


def test_poll_holds_cursor_when_adapter_absent(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (audit round 2, P1): with no telegram adapter registered,
    the cursor used to advance past every notice every 3s — all notices
    silently dropped. It must hold instead."""
    gateway = FakeGateway()
    gateway.notices = [_notice(1, "A")]
    bridge, _ = _bridge(tmp_path, gateway, monkeypatch)
    bridge.core.adapters.pop("telegram")

    asyncio.run(bridge.poll_once())
    assert bridge._cursor == 0
    # still held on the second round (and no exception)
    asyncio.run(bridge.poll_once())
    assert bridge._cursor == 0


def test_load_state_rejects_corrupt_cursor(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression (audit round 2, P2): a corrupted notice_cursor.json
    (non-int) used to be loaded verbatim, making every _notices_after SQL
    parameter wrong — a permanent poll error loop. It must reset to 0."""
    state_dir = tmp_path / "state" / "im_bridge"
    state_dir.mkdir(parents=True)
    (state_dir / "notice_cursor.json").write_text('{"oops": true}')
    bridge, _ = _bridge(tmp_path, FakeGateway(), monkeypatch)
    assert bridge._cursor == 0
