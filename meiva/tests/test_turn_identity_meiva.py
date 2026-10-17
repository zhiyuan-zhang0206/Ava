from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

import meiva
from base.agents.context import MeivaContext
from base.agents.context.identity import AgentIdentity
from meiva.sdk_surface import agent_identity, process_context
from meiva.tests.fixtures.pin_agent import pin_agent, pin_no_identity


@pytest.fixture(autouse=True)
def _reset_process_slots(monkeypatch: pytest.MonkeyPatch) -> None:
    pin_no_identity()
    monkeypatch.delenv("MEIVA_AGENT_ID", raising=False)


def test_turn_metadata_cannot_override_a_local_sdk_binding() -> None:
    pin_agent(
        11,
        owns_loop=True,
    )
    turn = MeivaContext(identity=AgentIdentity(22, True))
    assert agent_identity.require_agent_id(turn) == 22
    assert agent_identity.agent_id() == 11
    assert agent_identity.require_actor() == "agent:11"
    assert agent_identity.require_agent_id() == 11


def test_host_identity_requires_the_callers_explicit_context() -> None:
    context = MeivaContext(identity=AgentIdentity(33, True))
    assert agent_identity.agent_id() is None
    with pytest.raises(RuntimeError, match="no established agent identity"):
        agent_identity.require_agent_id()
    with pytest.raises(RuntimeError, match="established agent identity"):
        agent_identity.assert_self_action("terminate")
    assert agent_identity.require_agent_id(context) == 33
    assert agent_identity.require_actor(context) == "agent:33"
    assert getattr(meiva, "context", None) is None


def test_established_child_context_cannot_become_shared_host() -> None:
    pin_agent(5, owns_loop=False)
    with pytest.raises(RuntimeError, match="established SDK process context"):
        process_context.bind_host_process()
    assert agent_identity.require_agent_id() == 5
    assert process_context.is_host_process() is False


def test_turn_metadata_cannot_grant_a_launched_script_loop_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEIVA_AGENT_ID", "5")
    assert agent_identity.is_launched_child() is True
    turn = MeivaContext(identity=AgentIdentity(5, True))
    assert agent_identity.require_agent_id(turn) == 5
    with pytest.raises(RuntimeError, match="background script"):
        agent_identity.assert_self_action("restart")


def test_explicit_actor_is_independent_of_turn_metadata() -> None:
    pin_agent(
        None,
        actor="schedule:7",
    )
    turn = MeivaContext(identity=AgentIdentity(9, True))
    assert agent_identity.require_actor(turn) == "agent:9"
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
