"""Host turn metadata never selects the SDK's process-local identity."""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

import ava
from ava.sdk_surface import agent_identity
from base.agents.context import AvaContext
from base.agents.context.identity import AgentIdentity
from base.host.proc import run_bounded
from tests.fixtures.pin_agent import pin_agent, pin_no_identity


@pytest.fixture(autouse=True)
def _reset_process_slots(monkeypatch: pytest.MonkeyPatch) -> None:
    pin_no_identity()
    monkeypatch.delenv("AVA_AGENT_ID", raising=False)


def test_turn_metadata_cannot_override_a_local_sdk_binding() -> None:
    pin_agent(
        11,
        owns_loop=True,
    )
    turn = AvaContext(identity=AgentIdentity(22, True))
    assert agent_identity.require_agent_id(turn) == 22
    assert agent_identity.agent_id() == 11
    assert agent_identity.require_actor() == "agent:11"
    assert agent_identity.require_agent_id() == 11


def test_host_identity_requires_the_callers_explicit_context() -> None:
    context = AvaContext(identity=AgentIdentity(33, True))
    assert agent_identity.agent_id() is None
    with pytest.raises(RuntimeError, match="no established agent identity"):
        agent_identity.require_agent_id()
    with pytest.raises(RuntimeError, match="established agent identity"):
        agent_identity.assert_self_action("terminate")
    assert agent_identity.require_agent_id(context) == 33
    assert agent_identity.require_actor(context) == "agent:33"
    assert getattr(ava, "context", None) is None


def test_host_startup_refuses_env_context_and_attachment() -> None:
    code = """
import os
os.environ.pop("AVA_AGENT_ID", None)
import ava
import runpy
from base import config
class BootStoppedError(RuntimeError):
    pass
def stop_boot():
    raise BootStoppedError("after SDK posture")
config.ensure_eager = stop_boot
try:
    runpy.run_module("services.agent_runner.agent_host.daemon", run_name="__main__")
except BootStoppedError:
    pass
else:
    raise AssertionError("boot did not stop")
from ava.sdk_surface import agent_identity, process_context
os.environ["AVA_AGENT_ID"] = "5"
ava.bind_host_process()
assert ava.is_host_process()
assert agent_identity.is_launched_child() is False
assert getattr(ava, "context", None) is None
try:
    ava.external.attach("must-not-be-read")
except RuntimeError as exc:
    assert "native agent runtime" in str(exc)
else:
    raise AssertionError("host attached")
try:
    ava.bind_context(process_context.launched_context())
except RuntimeError as exc:
    assert "shared agent host" in str(exc)
else:
    raise AssertionError("host rebound")
try:
    ava.unbind_context()
except RuntimeError as exc:
    assert "startup posture" in str(exc)
else:
    raise AssertionError("host posture was released")
"""
    result = run_bounded(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr


def test_executable_host_rejects_inherited_child_identity_before_sdk_import() -> None:
    code = """
import os
import sys
import runpy
os.environ["AVA_AGENT_ID"] = "5"
try:
    runpy.run_module("services.agent_runner.agent_host.daemon", run_name="__main__")
except RuntimeError as exc:
    assert "cannot inherit a launched-agent identity" in str(exc)
else:
    raise AssertionError("host accepted inherited child identity")
assert "ava" not in sys.modules
"""
    result = run_bounded(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr


def test_established_child_context_cannot_become_shared_host() -> None:
    pin_agent(5, owns_loop=False)
    with pytest.raises(RuntimeError, match="established SDK process context"):
        ava.bind_host_process()
    assert agent_identity.require_agent_id() == 5
    assert ava.is_host_process() is False


def test_turn_metadata_cannot_grant_a_launched_script_loop_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AVA_AGENT_ID", "5")
    assert agent_identity.is_launched_child() is True
    turn = AvaContext(identity=AgentIdentity(5, True))
    assert agent_identity.require_agent_id(turn) == 5
    with pytest.raises(RuntimeError, match="background script"):
        agent_identity.assert_self_action("restart")


def test_explicit_actor_is_independent_of_turn_metadata() -> None:
    pin_agent(
        None,
        actor="schedule:7",
    )
    turn = AvaContext(identity=AgentIdentity(9, True))
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
