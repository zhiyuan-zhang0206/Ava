"""Explicit context branches retain the original work without task-local native getters."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest

from base.agents.context import AvaContext
from base.agents.incarnation.native_work_models import NativeWorkTarget
from base.native_process.runtime_incarnation import RuntimeIncarnation


def test_wrong_agent_incarnation_is_rejected() -> None:
    incarnation = RuntimeIncarnation(7, uuid4(), uuid4())
    with pytest.raises(RuntimeError, match="different agent"):
        incarnation.require_agent(8)
    assert incarnation.require_agent(7) is incarnation


async def test_context_branches_keep_work_and_incarnation_after_parent_replacement() -> None:
    incarnation = RuntimeIncarnation(7, uuid4(), uuid4())
    first = NativeWorkTarget(
        work_id=uuid4(),
        agent_id=7,
        machine="test",
        generation=incarnation.generation,
        owner=incarnation.owner,
        protocol=1,
    )
    second = first.model_copy(update={"work_id": uuid4()})
    original = AvaContext(original_incarnation=incarnation, native_work=first)
    child_ready, release_child = asyncio.Event(), asyncio.Event()

    async def copied(context: AvaContext) -> None:
        child_ready.set()
        await release_child.wait()
        assert context.original_incarnation is incarnation
        assert context.native_work is first
        nested = replace(context, native_work=second)
        assert nested.original_incarnation is incarnation and nested.native_work is second
        assert context.native_work is first

    copied_task = asyncio.create_task(copied(replace(original)))
    await child_ready.wait()
    with pytest.raises(RuntimeError, match="nested failure"):
        nested = replace(original, native_work=second)
        assert nested.original_incarnation is incarnation and nested.native_work is second
        raise RuntimeError("nested failure")
    assert original.original_incarnation is incarnation and original.native_work is first
    replacement = replace(original, original_incarnation=None, native_work=None)
    assert replacement.original_incarnation is None and replacement.native_work is None
    assert original.native_work is first
    release_child.set()
    await copied_task
