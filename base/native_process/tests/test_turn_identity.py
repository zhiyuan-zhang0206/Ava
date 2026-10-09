"""Native admission scopes retain original incarnations without providing an agent getter."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.native_process.turn_identity import (
    bind_native_work,
    bind_turn_identity,
    current_native_work_id,
    current_turn_incarnation,
)


def test_wrong_agent_incarnation_is_rejected() -> None:
    incarnation = RuntimeIncarnation(7, uuid4(), uuid4())
    with (
        pytest.raises(ValueError, match="different agent"),
        bind_turn_identity(8, incarnation=incarnation),
    ):
        pytest.fail("invalid admission was bound")
    assert current_turn_incarnation() is None


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
