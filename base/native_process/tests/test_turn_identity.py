"""The base-layer turn identity read: a bound turn wins over the AVA_AGENT_ID env identity, which is None when absent or malformed."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.native_process.turn_identity import (
    bind_native_work,
    bind_turn_identity,
    current_native_work_id,
    current_turn_agent_id,
    current_turn_incarnation,
    effective_agent_id,
)


@pytest.fixture(autouse=True)
def _unset_agent_id_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The env identity is the one ambient input these reads have besides the bound turn: start each test without it."""
    monkeypatch.delenv("AVA_AGENT_ID", raising=False)


class TestEffectiveAgentId:
    def test_unbound_no_env_is_none(self) -> None:
        assert effective_agent_id() is None
        assert current_turn_agent_id() is None

    def test_env_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AVA_AGENT_ID", "42")
        assert effective_agent_id() == 42

    def test_bound_wins_over_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AVA_AGENT_ID", "42")
        with bind_turn_identity(7):
            assert effective_agent_id() == 7
        assert effective_agent_id() == 42

    def test_malformed_env_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AVA_AGENT_ID", "not-a-number")
        assert effective_agent_id() is None


async def test_work_carrier_preserves_incarnation_nested_reset_and_task_copy() -> None:
    incarnation = RuntimeIncarnation(7, uuid4(), uuid4())
    first, second = uuid4(), uuid4()
    child_ready, release_child = asyncio.Event(), asyncio.Event()

    async def copied() -> None:
        child_ready.set()
        await release_child.wait()
        assert current_turn_incarnation() == incarnation
        assert current_native_work_id() == first
        with bind_native_work(second):
            assert current_turn_incarnation() == incarnation
            assert current_native_work_id() == second
        assert current_native_work_id() == first

    assert current_turn_incarnation() is None and current_native_work_id() is None
    with bind_turn_identity(7, incarnation=incarnation), bind_native_work(first):
        copied_task = asyncio.create_task(copied())
        await child_ready.wait()
        with pytest.raises(RuntimeError), bind_native_work(second):
            assert current_turn_incarnation() == incarnation
            assert current_native_work_id() == second
            raise RuntimeError("test nested reset")
        assert current_native_work_id() == first
        with bind_turn_identity(8):
            assert current_turn_incarnation() is None and current_native_work_id() is None
        assert current_turn_incarnation() == incarnation and current_native_work_id() == first
    assert current_turn_incarnation() is None and current_native_work_id() is None
    release_child.set()
    await copied_task
