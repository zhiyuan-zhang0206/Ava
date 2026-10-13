"""A manifest stored before the vocabulary rename is still claimed and sent by the Telegram adapter."""

import json
from typing import Any

import httpx
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from services.entrypoints.im_bridge.adapters.telegram import TelegramAdapter
from services.entrypoints.im_bridge.tests.slices import telegram_config
from services.entrypoints.im_bridge.tests.test_notice_outbox import make_core
from services.entrypoints.im_bridge.tests.test_timeline_outbox import pool as pool


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
