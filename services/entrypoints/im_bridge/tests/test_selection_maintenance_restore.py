"""Cold restoration precedes readiness without admitting held IM work."""

import datetime as dt
from typing import cast

import pytest

from base.deploy.maintenance import admission, pause_owner
from base.deploy.maintenance.state import CERTIFIED_PHASES, MaintenanceHold, MaintenancePhase
from services.entrypoints.im_bridge.core import IMBridgeCore
from services.entrypoints.im_bridge.state import _save_switch_state
from services.entrypoints.im_bridge.tests.task_scope import owned_tasks
from services.entrypoints.im_bridge.tests.test_im_bridge_core import (
    FakeGateway,
    FakePlainAdapter,
    _row,
    create_test_core,
)
from services.entrypoints.im_bridge.types import ChatState


async def assert_live_admission_held(
    core: IMBridgeCore,
    state: ChatState,
    upstream: FakeGateway,
) -> None:
    with pytest.raises(RuntimeError, match="IM selection is held"):
        await core._handle_chat(state, "held prompt")
    with pytest.raises(RuntimeError, match="IM switch acceptance is held"):
        await core._cmd_switch(state, "8")
    assert upstream.sent == []


def assert_restored_selection(core: IMBridgeCore) -> ChatState:
    state = core.chats[("telegram", "chat")]
    assert state.current_agent_id == 7
    assert ("telegram", "chat") in core._subscriptions
    assert core.outbound_store.selections() == {("telegram", "chat"): 7}
    return state


@pytest.mark.parametrize("phase", sorted(CERTIFIED_PHASES))
@pytest.mark.parametrize("legacy", [False, True], ids=["canonical", "legacy-bootstrap"])
async def test_cold_restoration_precedes_hold_release_without_admission_or_dispatch(
    phase: MaintenancePhase,
    legacy: bool,
) -> None:
    async with owned_tasks() as tasks:
        upstream = FakeGateway(agents=[_row(7), _row(8)])
        seeded = create_test_core(upstream, tasks=tasks)
        if legacy:
            _save_switch_state({"telegram:chat": 7})
        else:
            await seeded._cmd_switch(seeded._get_or_create_state("telegram", "chat"), "7")
        upstream.timeline = [
            {
                "kind": "agent_chat",
                "item_id": "1.0",
                "payload": "saved reply",
                "source_message_id": "stored-reply",
                "source_block_idx": 0,
            }
        ]

        when = dt.datetime.now(dt.UTC)
        initial = pause_owner.begin_maintenance("cold-start", when).snapshot
        assert initial.maintenance is not None
        held = MaintenanceHold(phase)
        pause_owner.change_maintenance("cold-start", when, initial.maintenance, held)
        assert admission.quiesced()

        restored = create_test_core(upstream, tasks=tasks)
        await restored.restore_subscriptions()
        state = assert_restored_selection(restored)
        subscription = restored._subscriptions[("telegram", "chat")]
        assert not subscription.done()
        assert pause_owner.read().maintenance == held

        await assert_live_admission_held(restored, state, upstream)
        await restored._catch_up(("telegram", "chat"), state, 7)
        await restored.outbound_worker.run_once()
        adapter = cast(FakePlainAdapter, restored.adapters["telegram"])
        assert adapter.sent == []

        # The operator releases the same journal after readiness. A canonical
        # selection delivers new output without a bridge restart or user command.
        pause_owner.change_maintenance("cold-start", when, held, held, resumed=True)
        assert not admission.quiesced()
        await restored.poll_timeline_outbound()
        assert restored._subscriptions[("telegram", "chat")] is subscription
        if legacy:
            # Importing a legacy choice must not qualify unknown historical output.
            assert adapter.sent == []
            await restored._cmd_switch(state, "7", replay_id="qualify-legacy")
            await restored.outbound_worker.run_once()
        assert adapter.sent == [("chat", "[Ava #7] saved reply")]


async def test_restoration_propagates_unknown_selection_fault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with owned_tasks() as tasks:
        _save_switch_state({"telegram:chat": 7})
        core = create_test_core(FakeGateway(), tasks=tasks)

        async def broken_account() -> str:
            raise RuntimeError("unexpected account fault")

        monkeypatch.setattr(core.adapters["telegram"], "outbound_account_id", broken_account)
        with pytest.raises(RuntimeError, match="unexpected account fault"):
            await core.restore_subscriptions()
        assert core._subscriptions == {}


async def test_restoration_rejects_invalid_maintenance_journal() -> None:
    async with owned_tasks() as tasks:
        path = pause_owner.state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"state":"paused","maintenance":{"phase":"unknown"}}')
        core = create_test_core(FakeGateway(), tasks=tasks)
        with pytest.raises(RuntimeError, match="unreadable pause owner"):
            await core.restore_subscriptions()
        assert core._subscriptions == {}
