"""Host turn metadata never selects the SDK's process-local identity."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest

import ava
from ava.sdk_surface import agent_identity
from base.native_process.turn_identity import bind_turn_identity, current_turn_agent_id
from tests.fixtures.pin_agent import exec_context, pin_agent, pin_no_identity


@pytest.fixture(autouse=True)
def _reset_process_slots(monkeypatch: pytest.MonkeyPatch) -> None:
    pin_no_identity()
    monkeypatch.delenv("AVA_AGENT_ID", raising=False)


def test_turn_metadata_cannot_override_a_local_sdk_binding() -> None:
    pin_agent(11, owns_loop=True)
    with bind_turn_identity(22):
        assert agent_identity.agent_id() == 11
        assert agent_identity.require_actor() == "agent:11"
    assert agent_identity.require_agent_id() == 11


def test_host_identity_requires_the_callers_explicit_context() -> None:
    context = exec_context(33)
    with bind_turn_identity(33):
        assert agent_identity.agent_id() is None
        with pytest.raises(RuntimeError, match="no established agent identity"):
            agent_identity.require_agent_id()
        with pytest.raises(RuntimeError, match="established agent identity"):
            agent_identity.assert_self_action("terminate")
        assert agent_identity.require_agent_id(context) == 33
        assert agent_identity.require_actor(context) == "agent:33"
    assert getattr(ava, "context", None) is None


def test_native_turn_cannot_bootstrap_env_identity_or_attach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ava.external import attach

    monkeypatch.setenv("AVA_AGENT_ID", "5")
    with bind_turn_identity(5):
        assert agent_identity.is_launched_child() is False
        assert getattr(ava, "context", None) is None
        with pytest.raises(RuntimeError, match="native agent runtime"):
            attach("must-not-be-read")


def test_turn_metadata_cannot_grant_a_launched_script_loop_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AVA_AGENT_ID", "5")
    assert agent_identity.is_launched_child() is True
    with bind_turn_identity(5), pytest.raises(RuntimeError, match="background script"):
        agent_identity.assert_self_action("restart")


def test_explicit_actor_is_independent_of_turn_metadata() -> None:
    pin_agent(None, actor="schedule:7")
    with bind_turn_identity(9):
        assert agent_identity.require_actor() == "schedule:7"
        assert agent_identity.require_actor() == "schedule:7"


def test_sdk_threads_use_one_local_binding_without_patching_thread_start() -> None:
    import threading

    def read_identity(_: int) -> int:
        return agent_identity.require_agent_id()

    start = threading.Thread.start
    pin_agent(88)
    with ThreadPoolExecutor(2) as pool:
        assert list(pool.map(read_identity, range(4))) == [88] * 4
    pin_no_identity()
    assert threading.Thread.start is start
    assert threading.Thread.start.__module__ == "threading"


def test_native_metadata_still_propagates_to_host_async_tasks() -> None:
    async def scenario() -> tuple[int | None, int | None]:
        async def read_turn() -> int | None:
            return current_turn_agent_id()

        with bind_turn_identity(77):
            task = asyncio.create_task(read_turn())
        return await task, current_turn_agent_id()

    assert asyncio.run(scenario()) == (77, None)
