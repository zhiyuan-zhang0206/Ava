"""Provider cutover, account replacement and command crash windows use native owners."""

import asyncio
from pathlib import Path
from typing import Any

import psycopg
import pytest

from services.entrypoints.im_bridge.adapters.weixin import WeixinAdapter, save_account
from services.entrypoints.im_bridge.ingress.store import WeixinIngressStore
from services.entrypoints.im_bridge.ingress.tests.conftest import NativeWeixin, message
from services.entrypoints.im_bridge.ingress.types import (
    IngressStatus,
    StalePollBindingError,
)


async def replace_account(native: NativeWeixin, account_id: str) -> WeixinAdapter:
    save_account(
        account_id=account_id,
        bot_token="isolated-test-token",  # noqa: S106
        user_id="owner",
        base_url="https://provider.example",
    )
    adapter = WeixinAdapter(native.core, client=native.adapter._http)
    native.core.register(adapter)
    store = WeixinIngressStore(native.pool)
    held = store.initialize("https://provider.example", account_id, None)
    store.begin_cutover(held, expected_cursor="")
    return adapter


async def test_entire_legacy_backlog_drains_without_execution_until_real_empty_commit(
    native_weixin: NativeWeixin, db_conn: psycopg.Connection
) -> None:
    adapter = await replace_account(native_weixin, "new-bot")
    native_weixin.provider_responses.extend(
        [
            {"ret": 0, "msgs": [message("/switch 1", "1")], "get_updates_buf": "one"},
            {"ret": 0, "msgs": [message("spawn:go", "2")], "get_updates_buf": "two"},
            {"ret": 0, "msgs": [message("old ordinary", "3")], "get_updates_buf": "three"},
            {"ret": 0, "get_updates_buf": "not-empty-proof"},
            {"ret": 0, "msgs": []},
            {"ret": 0, "msgs": [], "get_updates_buf": "verified-empty"},
        ]
    )
    cursor = ""
    for expected in ("one", "two", "three"):
        cursor, _ = await adapter._poll_once(cursor, 35000)
        assert cursor == expected
        assert db_conn.execute(
            "SELECT state FROM weixin_ingress_cursors WHERE account_id='new-bot'"
        ).fetchone() == ("draining",)
    with pytest.raises(TypeError, match="message list"):
        await adapter._poll_once(cursor, 35000)
    cursor, _ = await adapter._poll_once(cursor, 35000)
    assert cursor == "three"
    assert db_conn.execute(
        "SELECT state FROM weixin_ingress_cursors WHERE account_id='new-bot'"
    ).fetchone() == ("draining",)
    assert native_weixin.gateway_requests == []
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (0,)
    assert (
        db_conn.execute("SELECT status FROM weixin_ingress_receipts ORDER BY id").fetchall()
        == [("quarantined",)] * 3
    )
    cursor, _ = await adapter._poll_once(cursor, 35000)
    assert cursor == "verified-empty"
    assert db_conn.execute(
        "SELECT state,cursor FROM weixin_ingress_cursors WHERE account_id='new-bot'"
    ).fetchone() == ("active", "verified-empty")


async def test_same_peer_old_selection_and_reply_mode_do_not_authorize_new_account(
    native_weixin: NativeWeixin, db_conn: psycopg.Connection
) -> None:
    old_account = await native_weixin.adapter.outbound_account_id()
    native_weixin.core.notice_bridge._arm_reply_mode(
        "peer-1", str(native_weixin.agent_id), "99", account_id=old_account
    )
    adapter = await replace_account(native_weixin, "new-bot")
    native_weixin.provider_responses.append({"ret": 0, "msgs": [], "get_updates_buf": "new-floor"})
    await adapter._poll_once("", 35000)
    blocked = await adapter._handle_message(message("reply intended for old account", "4"))
    assert blocked.status == IngressStatus.QUARANTINED
    assert blocked.outcome_reason == "notice_reply_mode_account_unproven"
    assert native_weixin.core.notice_bridge.reply_target(
        "peer-1", "retained old mode", account_id=old_account
    ) == (native_weixin.agent_id, 99)
    # A command bypasses interception, but never borrows legacy selection as business proof.
    ordinary = await adapter._handle_message(message("/unknown skill", "5"))
    assert ordinary.status == IngressStatus.REJECTED
    assert ordinary.route.agent_id is None
    assert native_weixin.gateway_requests == []
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (0,)


async def test_account_caches_preserve_legacy_files_and_never_restore_old_peer_session(
    native_weixin: NativeWeixin, tmp_path: Path
) -> None:
    legacy_tokens = tmp_path / "state/im_bridge/weixin_context_tokens.json"
    legacy_activity = tmp_path / "state/im_bridge/weixin_activity.json"
    legacy_tokens.write_text('{"peer-1":"unbound-secret"}')
    legacy_activity.write_text('{"last_inbound":{"peer-1":9999999999}}')
    old = native_weixin.adapter
    old._tokens.set("peer-1", "account-specific-secret")
    old._mark_inbound("peer-1")
    adapter = await replace_account(native_weixin, "new-bot")
    adapter._tokens.restore()
    adapter._restore_activity()
    assert adapter._tokens.get("peer-1") is None
    assert "peer-1" not in adapter._last_inbound
    assert legacy_tokens.read_text() == '{"peer-1":"unbound-secret"}'
    assert legacy_activity.read_text() == '{"last_inbound":{"peer-1":9999999999}}'
    assert old._tokens.get("peer-1") == "account-specific-secret"


async def test_fresh_simultaneous_switch_calls_once_and_hint_failure_keeps_business_acceptance(
    native_weixin: NativeWeixin, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = native_weixin.core
    owner = core._handle_command
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def controlled(*args: Any, **kwargs: Any) -> object:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return await owner(*args, **kwargs)

    async def failed_hint(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("injected hint failure")

    monkeypatch.setattr(core, "_handle_command", controlled)
    monkeypatch.setattr(core, "_send", failed_hint)
    payload = message(f"/switch {native_weixin.agent_id}", "6")
    first = asyncio.create_task(native_weixin.adapter._handle_message(payload))
    await entered.wait()
    duplicate = await native_weixin.adapter._handle_message(payload)
    assert duplicate.status == IngressStatus.CLAIMED
    release.set()
    accepted = await first
    assert accepted.status == IngressStatus.ACCEPTED
    assert accepted.result is not None and accepted.result["agent_id"] == native_weixin.agent_id
    assert calls == 1
    assert await native_weixin.adapter._handle_message(payload) == accepted
    assert calls == 1


async def test_committed_command_then_completion_failure_recovers_uncertain_without_reexecution(
    native_weixin: NativeWeixin, monkeypatch: pytest.MonkeyPatch
) -> None:
    ingress, binding = await native_weixin.adapter._ensure_ingress()
    finish = ingress.store.finish

    def fail_finish(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("injected process failure after business owner")

    monkeypatch.setattr(ingress.store, "finish", fail_finish)
    payload = message(f"/switch {native_weixin.agent_id}", "7")
    with pytest.raises(RuntimeError, match="after business owner"):
        await native_weixin.adapter._handle_message(payload)
    before = len(native_weixin.gateway_requests)
    monkeypatch.setattr(ingress.store, "finish", finish)
    # A fresh adapter generation does not pretend the prior process has died.
    replacement = WeixinAdapter(native_weixin.core, client=native_weixin.adapter._http)
    native_weixin.core.register(replacement)
    recovered = await replacement._handle_message(payload)
    assert recovered.status == IngressStatus.UNCERTAIN
    assert recovered.outcome_reason == "command_attempt_unresolved"
    assert recovered.attempt_id is not None
    assert len(native_weixin.gateway_requests) == before
    with pytest.raises(StalePollBindingError):
        ingress.store.checkpoint(binding, "", "stale-ack")


@pytest.mark.parametrize("state", [None, 0, 1, 3, True, "2"])
async def test_incomplete_or_unknown_provider_snapshots_hold_cursor(
    native_weixin: NativeWeixin, db_conn: psycopg.Connection, state: object
) -> None:
    payload = message("unfinished", "8")
    payload["message_state"] = state
    native_weixin.provider_responses.append(
        {"ret": 0, "msgs": [payload], "get_updates_buf": "must-not-ack"}
    )
    with pytest.raises(ValueError, match="incomplete or unknown"):
        await native_weixin.adapter._poll_once("", 35000)
    assert db_conn.execute("SELECT cursor FROM weixin_ingress_cursors").fetchone() == ("",)
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (0,)


async def test_legacy_weixin_journal_stays_held_and_other_channels_continue(
    native_weixin: NativeWeixin, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.entrypoints.im_bridge.state import _load_outbox, _outbox_path

    path = _outbox_path()
    weixin = {
        "id": "legacy-weixin",
        "channel": "weixin",
        "chat_id": "peer-1",
        "agent_id": native_weixin.agent_id,
        "text": "retained original payload",
        "idempotency_key": "legacy-key",
        "enqueued_at": 1.0,
    }
    telegram = dict(weixin, id="legacy-telegram", channel="telegram", text="other channel")
    import json

    path.write_text(json.dumps(weixin) + "\n" + json.dumps(telegram) + "\n")
    delivered: list[tuple[int, str, str | None]] = []

    async def send(agent_id: int, text: str, *, idempotency_key: str | None = None) -> None:
        delivered.append((agent_id, text, idempotency_key))

    monkeypatch.setattr(native_weixin.core.gateway, "send_message", send)
    await native_weixin.core._replay_outbox_once()
    assert delivered == [(native_weixin.agent_id, "other channel", "legacy-key")]
    retained = _load_outbox()
    assert len(retained) == 1 and retained[0].__dict__ == weixin
    assert json.loads(path.read_text()) == weixin
    await native_weixin.core._replay_outbox_once()
    assert len(delivered) == 1
    assert json.loads(path.read_text()) == weixin


async def test_active_legacy_receipt_adoption_precedes_new_selection(
    native_weixin: NativeWeixin, db_conn: psycopg.Connection
) -> None:
    from base.agents.messages.chat_delivery import insert_chat_inbound_once
    from base.db import create_agent
    from services.entrypoints.im_bridge.ingress.identity import source_chat_key
    from services.entrypoints.im_bridge.ingress.types import ProviderSource

    original = insert_chat_inbound_once(
        db_conn,
        agent_id=native_weixin.agent_id,
        content="legacy accepted",
        source="user",
        payload=None,
        publish_wake=lambda _agent, _iid: True,
        client_message_id=source_chat_key(
            ProviderSource(
                namespace="https://provider.example",
                account_id="bot-id",
                sender_id="peer-1",
                message_id="9",
            )
        ),
    )
    later = create_agent(db_conn)
    # Current selection can change without rewriting history's original recipient.
    db_conn.execute(
        "UPDATE im_bridge_cursors SET push_agent_id=%s WHERE channel='weixin' AND chat_id='peer-1'",
        (later,),
    )
    db_conn.commit()
    receipt = await native_weixin.adapter._handle_message(message("legacy accepted", "9"))
    assert receipt.status == IngressStatus.ACCEPTED
    assert receipt.route.agent_id == native_weixin.agent_id
    assert receipt.inbound_id == original.inbound_id
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (later,)
    ).fetchone() == (0,)
    db_conn.execute("DELETE FROM inbound_messages WHERE id=%s", (original.inbound_id,))
    db_conn.commit()
    assert await native_weixin.adapter._handle_message(message("legacy accepted", "9")) == receipt
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (0,)


async def test_superseded_poll_waiting_for_provider_cannot_admit_or_checkpoint(
    native_weixin: NativeWeixin, db_conn: psycopg.Connection
) -> None:
    import httpx

    entered, release = asyncio.Event(), asyncio.Event()

    async def provider(_request: httpx.Request) -> httpx.Response:
        entered.set()
        await release.wait()
        return httpx.Response(
            200, json={"ret": 0, "msgs": [message("old poll", "24")], "get_updates_buf": "old-ack"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
        native_weixin.adapter._client = http
        old = asyncio.create_task(native_weixin.adapter._poll_once("", 35000))
        await entered.wait()
        store = WeixinIngressStore(native_weixin.pool)
        store.initialize("https://provider.example", "replacement-account", None)
        release.set()
        with pytest.raises(StalePollBindingError):
            await old
    assert db_conn.execute("SELECT count(*) FROM weixin_ingress_receipts").fetchone() == (0,)
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (0,)
    assert db_conn.execute(
        "SELECT cursor FROM weixin_ingress_cursors WHERE account_id='bot-id'"
    ).fetchone() == ("",)


async def test_unclaimed_source_recovers_frozen_route_before_new_poll(
    native_weixin: NativeWeixin, monkeypatch: pytest.MonkeyPatch
) -> None:
    ingress, _binding = await native_weixin.adapter._ensure_ingress()
    dispatch = ingress.dispatch

    async def before_first_claim(*_args: object) -> None:
        raise RuntimeError("injected interruption before command claim")

    monkeypatch.setattr(ingress, "dispatch", before_first_claim)
    payload = message(f"/switch {native_weixin.agent_id}", "25")
    with pytest.raises(RuntimeError, match="before command claim"):
        await native_weixin.adapter._handle_message(payload)
    monkeypatch.setattr(ingress, "dispatch", dispatch)
    replacement = WeixinAdapter(native_weixin.core, client=native_weixin.adapter._http)
    native_weixin.core.register(replacement)
    native_weixin.provider_responses.append(
        {"ret": 0, "msgs": [], "get_updates_buf": "after-recovery"}
    )
    cursor, _ = await replacement._poll_once("", 35000)
    assert cursor == "after-recovery"
    result = await replacement._handle_message(payload)
    assert result.status == IngressStatus.ACCEPTED
    assert result.route.text == f"/switch {native_weixin.agent_id}"
    assert result.result is not None and result.result["agent_id"] == native_weixin.agent_id


async def test_native_transport_timeout_is_not_provider_empty_cutover_proof(
    native_weixin: NativeWeixin, db_conn: psycopg.Connection
) -> None:
    import httpx

    adapter = await replace_account(native_weixin, "new-bot")

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("injected native long-poll timeout", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as http:
        adapter._client = http
        assert await adapter._poll_once("", 35000) == ("", 35000)
    assert db_conn.execute(
        "SELECT state,cursor FROM weixin_ingress_cursors WHERE account_id='new-bot'"
    ).fetchone() == ("draining", "")
    assert db_conn.execute("SELECT count(*) FROM weixin_ingress_receipts").fetchone() == (0,)


@pytest.mark.parametrize(
    "response",
    [
        {"ret": 0, "get_updates_buf": "unproven"},
        {"msgs": [], "get_updates_buf": "unproven"},
        {"ret": None, "msgs": [], "get_updates_buf": "unproven"},
        {"ret": True, "msgs": [], "get_updates_buf": "unproven"},
        {"ret": 0, "msgs": [], "get_updates_buf": "unproven", "longpolling_timeout_ms": True},
    ],
)
async def test_missing_or_invalid_provider_response_never_advances_unproven_cursor(
    native_weixin: NativeWeixin, db_conn: psycopg.Connection, response: dict[str, object]
) -> None:
    adapter = await replace_account(native_weixin, "new-bot")
    native_weixin.provider_responses.append(response)
    with pytest.raises(TypeError):
        await adapter._poll_once("", 35000)
    assert db_conn.execute(
        "SELECT state,cursor FROM weixin_ingress_cursors WHERE account_id='new-bot'"
    ).fetchone() == ("draining", "")
