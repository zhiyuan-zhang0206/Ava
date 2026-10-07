"""Account handover fences native effects and derived background subscriptions."""

import asyncio
from typing import Any

import psycopg
import pytest

from services.entrypoints.im_bridge.adapters.weixin import WeixinAdapter, save_account
from services.entrypoints.im_bridge.ingress.tests.conftest import NativeWeixin, message
from services.entrypoints.im_bridge.ingress.tests.test_recovery import replace_account
from services.entrypoints.im_bridge.ingress.types import StalePollBindingError


async def test_registration_before_new_poll_rejects_old_runtime_without_effects(
    native_weixin: NativeWeixin, db_conn: psycopg.Connection
) -> None:
    old = native_weixin.adapter
    await old._ensure_ingress()
    save_account(
        account_id="replacement",
        bot_token="isolated-test-token",  # noqa: S106
        user_id="owner",
        base_url="https://provider.example",
    )
    new = WeixinAdapter(native_weixin.core, client=old._http)
    native_weixin.core.register(new)
    assert new.selection_requires_account_proof is True
    for text, source in (("ordinary", "701"), (f"/switch {native_weixin.agent_id}", "702")):
        with pytest.raises(ValueError, match="adapter is superseded"):
            await old._handle_message(message(text, source))
    assert db_conn.execute("SELECT count(*) FROM weixin_ingress_receipts").fetchone() == (0,)
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (0,)
    assert native_weixin.gateway_requests == []
    assert native_weixin.provider_requests == []


async def test_replacement_after_claim_before_owner_transaction_rejects_epoch(
    native_weixin: NativeWeixin, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = native_weixin.core
    owner = core._handle_command
    entered, release = asyncio.Event(), asyncio.Event()

    async def held(*args: Any, **kwargs: Any) -> object:
        entered.set()
        await release.wait()
        return await owner(*args, **kwargs)

    monkeypatch.setattr(core, "_handle_command", held)
    first = asyncio.create_task(
        native_weixin.adapter._handle_message(message(f"/switch {native_weixin.agent_id}", "703"))
    )
    await entered.wait()
    new = await replace_account(native_weixin, "replacement")
    release.set()
    with pytest.raises(StalePollBindingError):
        await first
    account = await new.outbound_account_id()
    assert core.outbound_store.native_selection("weixin", account, "peer-1") is None
    assert db_conn.execute(
        "SELECT status FROM weixin_ingress_receipts WHERE message_id='703'"
    ).fetchone() == ("claimed",)
    assert native_weixin.provider_requests == []


async def test_new_active_account_clears_old_runtime_without_deleting_history(
    native_weixin: NativeWeixin, db_conn: psycopg.Connection
) -> None:
    core = native_weixin.core
    state = core.chats[("weixin", "peer-1")]
    assert state.current_agent_id == native_weixin.agent_id
    history = dict(core._switch_state)
    task = asyncio.create_task(asyncio.Event().wait())
    core._subscriptions[("weixin", "peer-1")] = task
    typing = asyncio.create_task(asyncio.Event().wait())
    core._typing_tasks[("weixin", "peer-1")] = typing
    new = await replace_account(native_weixin, "replacement")
    await new._poll_once("", 35000)  # Only a real empty provider response activates.
    runtime, binding = await new._ensure_ingress()
    await runtime.recover_subscriptions(binding)
    assert state.current_agent_id is None
    assert ("weixin", "peer-1") not in core._subscriptions
    assert ("weixin", "peer-1") not in core._typing_tasks
    await asyncio.sleep(0)
    assert task.cancelled() and typing.cancelled()
    assert core._switch_state == history
    assert (
        core.outbound_store.native_selection(
            "weixin", await native_weixin.adapter.outbound_account_id(), "peer-1"
        )
        == native_weixin.agent_id
    )
    assert (
        core.outbound_store.native_selection("weixin", await new.outbound_account_id(), "peer-1")
        is None
    )


async def test_sync_selection_cannot_bootstrap_unbound_legacy_agent(
    native_weixin: NativeWeixin,
) -> None:
    new = await replace_account(native_weixin, "replacement")
    state = native_weixin.core.chats[("weixin", "peer-1")]
    history = dict(native_weixin.core._switch_state)
    assert await native_weixin.core._sync_selection(state) is None
    assert state.current_agent_id is None
    assert native_weixin.core._switch_state == history
    assert (
        native_weixin.core.outbound_store.native_selection(
            "weixin", await new.outbound_account_id(), "peer-1"
        )
        is None
    )


async def test_native_selection_restores_fresh_core_derived_subscription(
    native_weixin: NativeWeixin, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = native_weixin.core
    core.chats.clear()
    restored: list[int | None] = []

    def subscribe(state: Any, **_kwargs: Any) -> None:
        restored.append(state.current_agent_id)

    monkeypatch.setattr(core, "_ensure_subscription", subscribe)
    runtime, binding = await native_weixin.adapter._ensure_ingress()
    await runtime.recover_subscriptions(binding)
    assert restored == [native_weixin.agent_id]
    assert core.chats[("weixin", "peer-1")].current_agent_id == native_weixin.agent_id


async def test_epoch_replacement_between_preflight_and_effect_is_rejected_in_sql(
    native_weixin: NativeWeixin, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    from services.entrypoints.im_bridge.ingress.store import WeixinIngressStore

    runtime, _binding = await native_weixin.adapter._ensure_ingress()
    owner = runtime.store.guard_claim_in_transaction
    entered, release = threading.Event(), threading.Event()

    def before_guard(conn: Any, **kwargs: Any) -> None:
        entered.set()
        assert release.wait(10)
        owner(conn, **kwargs)

    monkeypatch.setattr(runtime.store, "guard_claim_in_transaction", before_guard)
    task = asyncio.create_task(
        native_weixin.adapter._handle_message(message(f"/switch {native_weixin.agent_id}", "704"))
    )
    assert await asyncio.to_thread(entered.wait, 10)
    try:
        await asyncio.to_thread(
            WeixinIngressStore(native_weixin.pool).initialize,
            "https://provider.example",
            "replacement",
            None,
        )
    finally:
        release.set()
    with pytest.raises(StalePollBindingError):
        await task
    assert db_conn.execute(
        "SELECT count(*) FROM im_bridge_outbound_replays WHERE replay_id LIKE 'weixin%'"
    ).fetchone() == (0,)
    assert native_weixin.provider_requests == []


async def test_effect_transaction_holds_epoch_lock_until_original_account_commit(
    native_weixin: NativeWeixin, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    runtime, _binding = await native_weixin.adapter._ensure_ingress()
    owner = runtime.store.guard_claim_in_transaction
    entered, release = threading.Event(), threading.Event()

    def after_guard(conn: Any, **kwargs: Any) -> None:
        owner(conn, **kwargs)
        entered.set()
        assert release.wait(10)

    monkeypatch.setattr(runtime.store, "guard_claim_in_transaction", after_guard)
    task = asyncio.create_task(
        native_weixin.adapter._handle_message(message(f"/switch {native_weixin.agent_id}", "705"))
    )
    assert await asyncio.to_thread(entered.wait, 10)
    try:
        with pytest.raises(psycopg.errors.LockNotAvailable), db_conn.transaction():
            db_conn.execute("SELECT epoch FROM weixin_ingress_bindings FOR UPDATE NOWAIT")
    finally:
        release.set()
    accepted = await task
    assert accepted.status.value == "accepted"
    assert accepted.result is not None and accepted.result["agent_id"] == native_weixin.agent_id
    assert await native_weixin.adapter.outbound_account_id() == accepted.route.command_account


async def test_postcommit_subscription_failure_preserves_acceptance_and_loop_recovers(
    native_weixin: NativeWeixin, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, binding = await native_weixin.adapter._ensure_ingress()
    owner = runtime.recover_subscriptions

    async def unavailable(_binding: Any) -> None:
        raise RuntimeError("injected derived subscription failure")

    monkeypatch.setattr(runtime, "recover_subscriptions", unavailable)
    accepted = await native_weixin.adapter._handle_message(
        message("retained despite tail failure", "706")
    )
    assert accepted.status.value == "accepted"
    assert db_conn.execute(
        "SELECT inbound_id FROM weixin_ingress_receipts WHERE id=%s", (accepted.id,)
    ).fetchone() == (accepted.inbound_id,)
    assert (
        await native_weixin.adapter._handle_message(message("retained despite tail failure", "706"))
        == accepted
    )
    monkeypatch.setattr(runtime, "recover_subscriptions", owner)
    native_weixin.core.chats.clear()
    await runtime.recover_subscriptions(binding)
    assert native_weixin.core.chats[("weixin", "peer-1")].current_agent_id == native_weixin.agent_id
