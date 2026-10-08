"""Normal notice acceptance uses real receipts, cutover and shared dispatch."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pytest
from psycopg import Connection
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from services.entrypoints.im_bridge.adapters.telegram import TelegramAdapter
from services.entrypoints.im_bridge.core import IMBridgeCore
from services.entrypoints.im_bridge.outbound.store import IMOutboxStore
from services.entrypoints.im_bridge.outbound.types import (
    NoticePollImportReason,
    OutboundAdapterKind,
    OutboundChunk,
    PreparedOutboundSend,
)
from services.entrypoints.im_bridge.tests.slices import im_bridge_config, telegram_config
from services.entrypoints.im_bridge.tests.test_im_bridge_core import FakeGateway
from services.entrypoints.im_bridge.tests.test_timeline_outbox import RecordingAdapter
from services.entrypoints.im_bridge.tests.test_timeline_outbox import pool as pool
from tests.fixtures.units import spawn_agent


class NoticeAdapter(RecordingAdapter):
    def __init__(self) -> None:
        super().__init__("bot")
        self.recipient: str | None = "owner-a"
        self.deliveries: list[tuple[str, str, tuple[tuple[str, str], ...] | None]] = []

    async def prepare_notice_owner(
        self, text: str, buttons: tuple[tuple[str, str], ...]
    ) -> tuple[str, PreparedOutboundSend]:
        if self.recipient is None:
            raise NotImplementedError("notice owner unavailable")
        return self.recipient, PreparedOutboundSend(
            adapter_kind=OutboundAdapterKind.TELEGRAM,
            account_id=self.account,
            chunks=(OutboundChunk(text=text),),
            markdown=False,
            buttons=buttons,
        )

    async def send_prepared_outbound(self, chat_id: str, prepared: PreparedOutboundSend) -> None:
        self.deliveries.append((chat_id, prepared.chunks[0].text, prepared.buttons))
        await super().send_prepared_outbound(chat_id, prepared)


def make_core(pool: ConnectionPool) -> tuple[IMBridgeCore, NoticeAdapter]:
    core = IMBridgeCore(im_bridge_config(), FakeGateway(), db_pool=pool)  # type: ignore[arg-type]
    adapter = NoticeAdapter()
    core.register(adapter)
    return core, adapter


def insert_notice(conn: Connection, agent: int, local: int, title: str = "notice") -> int:
    row = conn.execute(
        "INSERT INTO agent_notices (agent_id,local_id,title,content,priority,require_response,blocking,expire_at) "
        "VALUES (%s,%s,%s,'body','P2',false,false,now()+interval '1 day') RETURNING id",
        (agent, local, title),
    ).fetchone()
    assert row is not None
    return int(row[0])


def receipts(pool: ConnectionPool) -> list[tuple[Any, ...]]:
    with pool.connection() as conn:
        return conn.execute(
            "SELECT notice_id,decision,request,intent_ids FROM im_bridge_notice_acceptances ORDER BY notice_id"
        ).fetchall()


async def test_new_install_accepts_atomically_and_worker_sends_frozen_target_buttons(
    pool: ConnectionPool,
) -> None:
    core, adapter = make_core(pool)
    core.notice_bridge.initialize_poll()  # The daemon initializes before readiness, not a user command.
    with pool.connection() as conn:
        notice = insert_notice(conn, spawn_agent(), 0)
    await core.notice_bridge.poll_once()
    accepted = receipts(pool)
    assert accepted[0][0] == notice and accepted[0][1] == "queued"
    assert adapter.deliveries == [], "normal poll must not contact the provider"
    frozen = accepted[0][2]["intent"]
    assert frozen["chat_id"] == "owner-a" and frozen["prepared"]["account_id"] == "bot"
    assert frozen["prepared"]["buttons"][0][1].endswith(f":{notice}")
    adapter.recipient = "owner-b"
    await core.notice_bridge.poll_once()
    await core.outbound_worker.run_once()
    assert len(adapter.deliveries) == 1 and adapter.deliveries[0][0] == "owner-a"
    assert adapter.deliveries[0][2] == tuple(
        tuple(button) for button in frozen["prepared"]["buttons"]
    )
    assert len(receipts(pool)) == 1


async def test_two_connections_reverse_commit_do_not_skip_late_lower_id(
    pool: ConnectionPool,
) -> None:
    core, adapter = make_core(pool)
    core.notice_bridge.poll_store.initialize_notice_poll(0)
    agent = spawn_agent()
    with pool.connection() as low:
        low_id = insert_notice(low, agent, 0, "low")
        with pool.connection() as high:
            high_id = insert_notice(high, agent, 1, "high")
        assert high_id > low_id
        await core.notice_bridge.poll_once()
        assert [r[0] for r in receipts(pool)] == [high_id]
        assert core.notice_bridge._cursor == high_id
    await core.notice_bridge.poll_once()
    assert [r[0] for r in receipts(pool)] == [low_id, high_id]
    assert core.notice_bridge._cursor == high_id, "high ID is diagnostic, never eligibility"
    for _ in range(2):
        await core.outbound_worker.run_once()
    assert len(adapter.deliveries) == 2


@pytest.mark.parametrize("raw", [None, True, -1, {}, "invalid"])
async def test_missing_or_corrupt_legacy_cursor_retains_old_range_only(
    pool: ConnectionPool, tmp_path: Path, raw: Any
) -> None:
    with pool.connection() as conn:
        old = insert_notice(conn, spawn_agent(), 0, "old history")
    if raw is not None:
        path = tmp_path / "state" / "im_bridge" / "notice_cursor.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(raw))
    core, adapter = make_core(pool)
    await core.notice_bridge.poll_once()
    assert not receipts(pool) and not adapter.deliveries
    floor, _, reason = core.notice_bridge.poll_store.initialize_notice_poll(0)
    assert floor == old and reason == NoticePollImportReason.LEGACY_HISTORY_UNKNOWN
    with pool.connection() as conn:
        new = insert_notice(conn, spawn_agent(), 0, "new range")
    await core.notice_bridge.poll_once()
    assert [r[0] for r in receipts(pool)] == [new]
    await core.outbound_worker.run_once()
    assert "new range" in adapter.deliveries[0][1]
    # Explicit listing remains available; it is intentionally a different producer.
    assert any(n["id"] == old for n in core.notice_bridge._open_notices())


async def test_valid_legacy_cursor_imports_once_preserving_skip(
    pool: ConnectionPool, tmp_path: Path
) -> None:
    agent = spawn_agent()
    with pool.connection() as conn:
        old = insert_notice(conn, agent, 0, "old")
        new = insert_notice(conn, agent, 1, "new")
    path = tmp_path / "state" / "im_bridge" / "notice_cursor.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(old))
    core, _ = make_core(pool)
    await core.notice_bridge.poll_once()
    assert [r[0] for r in receipts(pool)] == [new]
    assert core.notice_bridge.poll_store.initialize_notice_poll(999999)[0] == old
    assert (
        core.notice_bridge.poll_store.initialize_notice_poll(None)[2]
        == NoticePollImportReason.LEGACY_CURSOR
    )


async def test_filtered_decision_commits_without_owner_and_does_not_later_replay(
    pool: ConnectionPool,
) -> None:
    core, adapter = make_core(pool)
    core.notice_bridge.poll_store.initialize_notice_poll(0)
    with pool.connection() as conn:
        notice = insert_notice(conn, spawn_agent(), 0)
    adapter.recipient = None
    core.notice_bridge._filters = {"min_priority": "P1", "agent": None}
    await core.notice_bridge.poll_once()
    assert receipts(pool)[0][0:2] == (notice, "filtered")
    assert receipts(pool)[0][3] == []
    core.notice_bridge._filters = {"min_priority": None, "agent": None}
    adapter.recipient = "owner-a"
    await core.notice_bridge.poll_once()
    await core.outbound_worker.run_once()
    assert not adapter.deliveries


async def test_no_owner_holds_without_fake_receipt_then_accepts_when_available(
    pool: ConnectionPool,
) -> None:
    core, adapter = make_core(pool)
    core.notice_bridge.poll_store.initialize_notice_poll(0)
    with pool.connection() as conn:
        insert_notice(conn, spawn_agent(), 0)
    adapter.recipient = None
    await core.notice_bridge.poll_once()
    assert not receipts(pool)
    with pool.connection() as conn:
        assert conn.execute("SELECT count(*) FROM im_bridge_outbound_intents").fetchone() == (0,)
    adapter.recipient = "owner-a"
    await core.notice_bridge.poll_once()
    assert len(receipts(pool)) == 1


async def test_acceptance_rollback_then_lost_response_converge_without_inline_send(
    pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    core, adapter = make_core(pool)
    core.notice_bridge.poll_store.initialize_notice_poll(0)
    with pool.connection() as conn:
        insert_notice(conn, spawn_agent(), 0)
    insert = IMOutboxStore.insert_intent

    def rollback(conn: Connection, intent: Any) -> int:
        insert(conn, intent)
        raise RuntimeError("crash after insert before receipt commit")

    monkeypatch.setattr(IMOutboxStore, "insert_intent", staticmethod(rollback))
    await core.notice_bridge.poll_once()
    assert not receipts(pool)
    with pool.connection() as conn:
        assert conn.execute("SELECT count(*) FROM im_bridge_outbound_intents").fetchone() == (0,)
        assert conn.execute(
            "SELECT accepted_notice_id FROM im_bridge_notice_poll_state"
        ).fetchone() == (0,)
    monkeypatch.setattr(IMOutboxStore, "insert_intent", staticmethod(insert))
    accept = core.notice_bridge.poll_store.accept_notice

    def lost(*args: Any, **kwargs: Any) -> Any:
        accept(*args, **kwargs)
        raise RuntimeError("response lost after durable acceptance")

    monkeypatch.setattr(core.notice_bridge.poll_store, "accept_notice", lost)
    await core.notice_bridge.poll_once()
    assert len(receipts(pool)) == 1 and core.notice_bridge._cursor == 0
    monkeypatch.setattr(core.notice_bridge.poll_store, "accept_notice", accept)
    await core.notice_bridge.poll_once()
    await core.outbound_worker.run_once()
    assert len(adapter.deliveries) == 1 and core.notice_bridge._cursor > 0


async def test_concurrent_normal_acceptances_freeze_first_target_without_duplicate(
    pool: ConnectionPool,
) -> None:
    core, adapter = make_core(pool)
    core.notice_bridge.poll_store.initialize_notice_poll(0)
    with pool.connection() as conn:
        notice = insert_notice(conn, spawn_agent(), 0)
    snapshot = core.notice_bridge._notices_after(0)[0]

    def accept(_index: int) -> None:
        asyncio.run(core.notice_bridge._accept_normal_notice(snapshot))

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(accept, range(2)))
    assert len(receipts(pool)) == 1 and len(receipts(pool)[0][3]) == 1
    adapter.error = httpx.ReadError("https://api.telegram.org/botSECRET/sendMessage")
    await core.outbound_worker.run_once()
    adapter.error = None
    await core.notice_bridge.poll_once()
    await core.outbound_worker.run_once()
    assert len(adapter.deliveries) == 1
    with pool.connection() as conn:
        status = conn.execute(
            "SELECT status,outcome_reason FROM im_bridge_outbound_intents"
        ).fetchone()
        assert status is not None and status[0] == "uncertain" and "SECRET" not in str(status)
    assert receipts(pool)[0][0] == notice


async def test_legacy_stored_manifest_is_claimed_and_dispatched_after_vocabulary_rename(
    pool: ConnectionPool,
) -> None:
    # Original timeline request JSON; no new class names or source fields.
    request = {
        "channel": "telegram",
        "chat_id": "42",
        "agent_id": 7,
        "source": {"kind": "message", "identity": "legacy-stored", "block_idx": 0},
        "prepared": {
            "adapter_kind": "telegram-v1",
            "account_id": "123",
            "chunks": [{"text": "legacy", "fallback_text": None, "html": False}],
            "markdown": False,
            "buttons": None,
        },
        "replay_id": "",
    }
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO im_bridge_outbound_intents "
            "(channel,account_id,chat_id,agent_id,source_kind,source_id,block_idx,request) "
            "VALUES ('telegram','123','42',7,'message','legacy-stored',0,%s)",
            (Jsonb(request),),
        )
    sent: list[dict[str, Any]] = []

    def handler(call: httpx.Request) -> httpx.Response:
        if call.url.path.endswith("getMe"):
            return httpx.Response(200, json={"ok": True, "result": {"id": 123}})
        sent.append(json.loads(call.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

    core, _ = make_core(pool)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = TelegramAdapter(
            core,
            telegram_config(telegram_bot_token="TEST-TOKEN", telegram_owner_id=42),  # noqa: S106 - mock-only credential
            client=client,
        )
        core.register(adapter)
        await core.outbound_worker.run_once()
    assert sent == [{"chat_id": "42", "text": "legacy"}]
    with pool.connection() as conn:
        assert conn.execute("SELECT status FROM im_bridge_outbound_intents").fetchone() == ("sent",)
