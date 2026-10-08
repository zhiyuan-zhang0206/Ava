"""Replay receipts recover acceptance without undoing a later chat selection."""

import asyncio
from threading import Event
from typing import Any, cast

import pytest

from services.entrypoints.im_bridge.core import IMBridgeCore
from services.entrypoints.im_bridge.outbound.types import OutboundIdentityConflictError
from services.entrypoints.im_bridge.tests.task_scope import owned_tasks
from services.entrypoints.im_bridge.tests.test_im_bridge_core import (
    FakeGateway,
    FakePlainAdapter,
    _row,
    create_test_core,
)
from services.entrypoints.im_bridge.types import ChatState


def gateway() -> FakeGateway:
    return FakeGateway(agents=[_row(7), _row(8)])


async def test_old_replay_cannot_undo_later_selection_or_depend_on_old_liveness() -> None:
    async with owned_tasks() as _owned_tasks:
        upstream = gateway()
        core = create_test_core(upstream, tasks=_owned_tasks)
        state = core._get_or_create_state("telegram", "chat")
        await core._cmd_switch(state, "7", replay_id="A")
        await core._cmd_switch(state, "8", replay_id="B")
        upstream.agents = [_row(8)]  # Accepted A must recover even when its target vanished.
        assert await core._cmd_switch(state, "7", replay_id="A") == []
        assert state.current_agent_id == 8
        assert core.outbound_store.selections() == {("telegram", "chat"): 8}
        with pytest.raises(OutboundIdentityConflictError):
            await core._cmd_switch(state, "8", replay_id="A")
        with core.outbound_store._pool().connection() as conn:
            assert conn.execute("SELECT count(*) FROM im_bridge_outbound_replays").fetchone() == (
                2,
            )


async def test_accepted_switch_recovers_after_commit_before_json_cache_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with owned_tasks() as _owned_tasks:
        core = create_test_core(gateway(), tasks=_owned_tasks)
        state = core._get_or_create_state("telegram", "chat")

        def crashed(_self: Any, _state: Any, _selected: Any):
            raise RuntimeError("lost process before JSON selection cache write")

        original = IMBridgeCore._apply_selection
        monkeypatch.setattr(IMBridgeCore, "_apply_selection", crashed)
        with pytest.raises(RuntimeError, match="lost process"):
            await core._cmd_switch(state, "7", replay_id="A")
        monkeypatch.setattr(IMBridgeCore, "_apply_selection", original)
        restarted = create_test_core(gateway(), tasks=_owned_tasks)
        await restarted.restore_subscriptions()
        assert restarted.chats[("telegram", "chat")].current_agent_id == 7
        state2 = restarted.chats[("telegram", "chat")]
        assert await restarted._cmd_switch(state2, "7", replay_id="A") == []
        with restarted.outbound_store._pool().connection() as conn:
            assert conn.execute("SELECT count(*) FROM im_bridge_outbound_replays").fetchone() == (
                1,
            )


async def test_recipient_mutex_orders_old_receipt_read_and_later_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with owned_tasks() as _owned_tasks:
        core = create_test_core(gateway(), tasks=_owned_tasks)
        state = core._get_or_create_state("telegram", "chat")
        await core._cmd_switch(state, "7", replay_id="A")
        read, release = Event(), Event()
        original = core.outbound_store.lookup_replay

        def delayed(*args: Any):
            result = original(*args)
            if args[3] == "A":
                read.set()
                if not release.wait(5):
                    raise RuntimeError("test interleaving release timed out")
            return result

        monkeypatch.setattr(core.outbound_store, "lookup_replay", delayed)
        old = asyncio.create_task(core._cmd_switch(state, "7", replay_id="A"))
        assert await asyncio.to_thread(read.wait, 5)
        newer = asyncio.create_task(core._cmd_switch(state, "8", replay_id="B"))
        await asyncio.sleep(0.02)
        assert not newer.done(), "B cannot apply between A's canonical read and cache apply"
        release.set()
        await asyncio.gather(old, newer)
        assert state.current_agent_id == 8


async def test_status_clear_is_cas_and_survives_restart() -> None:
    async with owned_tasks() as _owned_tasks:
        core = create_test_core(gateway(), tasks=_owned_tasks)
        state = core._get_or_create_state("telegram", "chat")
        await core._cmd_switch(state, "7", replay_id="A")
        cast(FakeGateway, core.gateway).agents = []
        await core._cmd_status(state)
        assert state.current_agent_id is None
        restarted = create_test_core(gateway(), tasks=_owned_tasks)
        await restarted.restore_subscriptions()
        assert ("telegram", "chat") not in restarted._subscriptions
        assert restarted.outbound_store.selections() == {("telegram", "chat"): None}
        assert not core.outbound_store.clear_selection("telegram", "test-account", "chat", 7)


async def test_empty_switch_accepts_first_later_output_without_another_command() -> None:
    async with owned_tasks() as _owned_tasks:
        upstream = gateway()
        core = create_test_core(upstream, tasks=_owned_tasks)
        state = core._get_or_create_state("telegram", "chat")
        await core._cmd_switch(state, "7", replay_id="button-event")
        upstream.timeline = [
            {
                "kind": "agent_chat",
                "item_id": "1.0",
                "payload": "first output",
                "source_message_id": "persisted-first",
                "source_block_idx": 0,
            }
        ]
        await core.poll_timeline_outbound()
        assert cast(FakePlainAdapter, core.adapters["telegram"]).sent == [
            ("chat", "[Ava #7] first output")
        ]


@pytest.mark.parametrize("json_agent", [8, None])
async def test_legacy_json_choice_bootstraps_once_without_reviving_old_cursor(
    json_agent: int | None,
) -> None:
    async with owned_tasks() as _owned_tasks:
        from services.entrypoints.im_bridge.cursor_store import PushWatermark
        from services.entrypoints.im_bridge.state import _save_switch_state

        core = create_test_core(gateway(), tasks=_owned_tasks)
        core.cursor_store.save_push("telegram", "chat", 7, PushWatermark(None, "20.0"))
        _save_switch_state({"telegram:chat": json_agent} if json_agent is not None else {})
        restarted = create_test_core(gateway(), tasks=_owned_tasks)
        await restarted.restore_subscriptions()
        assert restarted.chats[("telegram", "chat")].current_agent_id == json_agent
        with restarted.outbound_store._pool().connection() as conn:
            assert conn.execute(
                "SELECT push_agent_id,push_item_id,push_initialized,push_account_id FROM im_bridge_cursors"
            ).fetchone() == (json_agent, None, False, "test-account")
        # A stale JSON cache cannot override the bound owner on a later restart.
        _save_switch_state({"telegram:chat": 7})
        again = create_test_core(gateway(), tasks=_owned_tasks)
        await again.restore_subscriptions()
        assert again.chats[("telegram", "chat")].current_agent_id == json_agent


async def test_spawn_switch_button_empty_receipt_then_first_output() -> None:
    async with owned_tasks() as _owned_tasks:
        from services.entrypoints.im_bridge.types import SpawnDraft

        upstream = FakeGateway(agents=[_row(777)])
        core = create_test_core(upstream, tasks=_owned_tasks)
        state = core._get_or_create_state("telegram", "chat")
        state.spawn_draft = SpawnDraft()
        receipt = await core._spawn_execute(state)
        assert receipt.buttons and receipt.buttons[0][1] == "/switch 777"
        await core._handle_command(state, receipt.buttons[0][1], replay_id="spawn-switch-click")
        await core._handle_chat(state, "first prompt", "prompt-event")
        upstream.timeline = [
            {
                "kind": "agent_chat",
                "item_id": "1.0",
                "payload": "first answer",
                "source_message_id": "stored-answer",
                "source_block_idx": 0,
            }
        ]
        await core.poll_timeline_outbound()
        assert upstream.sent == [(777, "first prompt", "user")]
        assert cast(FakePlainAdapter, core.adapters["telegram"]).sent == [
            ("chat", "[Ava #777] first answer")
        ]


async def test_postcommit_acceptance_response_loss_does_not_lose_or_duplicate_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with owned_tasks() as _owned_tasks:
        upstream = FakeGateway(
            timeline=[{"kind": "agent_chat", "item_id": "1.0", "payload": "reply"}]
        )
        core = create_test_core(upstream, tasks=_owned_tasks)
        state = ChatState("telegram", "chat", current_agent_id=7)
        original = core.outbound_store.accept

        def lost(*args: Any, **kwargs: Any):
            original(*args, **kwargs)
            raise RuntimeError("lost acceptance response after commit")

        monkeypatch.setattr(core.outbound_store, "accept", lost)
        with pytest.raises(RuntimeError, match="lost acceptance"):
            await core._push_snapshot(("telegram", "chat"), state, {})
        assert core._last_pushed == {}
        monkeypatch.setattr(core.outbound_store, "accept", original)
        await core._push_snapshot(("telegram", "chat"), state, {})
        await core.outbound_worker.run_once()
        assert cast(FakePlainAdapter, core.adapters["telegram"]).sent == [
            ("chat", "[Ava #7] reply")
        ]


async def test_live_sse_snapshot_is_only_a_wakeup_and_periodic_pull_catches_commit() -> None:
    async with owned_tasks() as _owned_tasks:
        upstream = gateway()
        core = create_test_core(upstream, tasks=_owned_tasks)
        state = core._get_or_create_state("telegram", "chat")
        await core._cmd_switch(state, "7", replay_id="empty")
        live = {
            "kind": "agent_chat",
            "item_id": "1.0",
            "payload": "not committed",
            "source_message_id": "new",
            "source_block_idx": 0,
        }
        await core._push_snapshot(("telegram", "chat"), state, {"items": [live]})
        assert core.outbound_store.pending_streams({"telegram": "test-account"}) == []
        upstream.timeline = [dict(live, payload="committed")]
        await core.poll_timeline_outbound()  # No second SSE event is required.
        assert cast(FakePlainAdapter, core.adapters["telegram"]).sent == [
            ("chat", "[Ava #7] committed")
        ]


async def test_live_sse_mutex_orders_acceptance_before_clear_and_late_old_producer_stays_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with owned_tasks() as _owned_tasks:
        from services.entrypoints.im_bridge.tests.test_timeline_outbox import candidate

        upstream = gateway()
        core = create_test_core(upstream, tasks=_owned_tasks)
        state = core._get_or_create_state("telegram", "chat")
        await core._cmd_switch(state, "7", replay_id="initial")
        upstream.timeline = [
            {
                "kind": "agent_chat",
                "item_id": "1.0",
                "payload": "live",
                "source_message_id": "live-id",
                "source_block_idx": 0,
            }
        ]
        started, release = asyncio.Event(), asyncio.Event()
        adapter = core.adapters["telegram"]
        original = adapter.prepare_timeline

        async def paused(text: str):
            started.set()
            await release.wait()
            return await original(text)

        monkeypatch.setattr(adapter, "prepare_timeline", paused)
        live = asyncio.create_task(core._push_snapshot(("telegram", "chat"), state, {}))
        await started.wait()
        upstream.agents = []
        clear = asyncio.create_task(core._cmd_status(state))
        await asyncio.sleep(0.02)
        assert not clear.done(), (
            "selection clear cannot interleave a live SSE's canonical acceptance"
        )
        release.set()
        await asyncio.gather(live, clear)
        late = core.outbound_store.accept(
            "telegram", "test-account", "chat", 7, [candidate(2, account="test-account")]
        )
        assert late.blocked and not late.intent_ids and late.selected_agent_id is None
        assert core.outbound_store.selections() == {("telegram", "chat"): None}
        with core.outbound_store._pool().connection() as conn:
            assert conn.execute("SELECT count(*) FROM im_bridge_outbound_intents").fetchone() == (
                1,
            )
