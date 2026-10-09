"""Explicit invocation contexts retain their original resource and admission owners."""

import asyncio
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from base.agents.context import AvaContext
from base.agents.context.identity import AgentIdentity
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.native_process.turn_identity import HostedTurnResources, hosted_resources_settled


async def test_concurrent_turn_completion_cannot_discharge_another_owner() -> None:
    path = Path("same-request")
    first, second = HostedTurnResources(), HostedTurnResources()
    first_owner, second_owner = object(), object()
    first.unresolved[path], second.unresolved[path] = first_owner, second_owner
    started = asyncio.Event()

    async def complete_first() -> None:
        await started.wait()
        assert first.complete(path, first_owner)

    async def inspect_second() -> None:
        started.set()
        await asyncio.sleep(0)
        assert not second.complete(path, first_owner)
        assert second.unresolved[path] is second_owner
        assert not hosted_resources_settled(second)

    await asyncio.gather(complete_first(), inspect_second())
    assert hosted_resources_settled(first)
    assert second.complete(path, second_owner)


def test_late_completion_preserves_replaced_resource_evidence() -> None:
    resources = HostedTurnResources()
    path = Path("request")
    prior, successor = object(), object()
    resources.unresolved[path] = prior
    resources.unresolved[path] = successor
    assert not resources.complete(path, prior)
    assert not resources.changed.is_set()
    assert resources.unresolved[path] is successor
    assert resources.complete(path, successor)
    assert resources.changed.is_set()


def test_copied_context_keeps_original_admission_and_cleanup_scope() -> None:
    incarnation = RuntimeIncarnation(17, uuid4(), uuid4())
    resources = HostedTurnResources()
    context = AvaContext(
        identity=AgentIdentity(17, True),
        original_incarnation=incarnation,
        hosted_resources=resources,
    )
    copied = replace(context, recall_log_key=b"copied-run")
    assert copied.require_original_incarnation(17) is incarnation
    assert copied.hosted_resources is resources
    with pytest.raises(RuntimeError, match="different agent"):
        copied.require_original_incarnation(18)
    with pytest.raises(RuntimeError, match="no original RuntimeIncarnation"):
        AvaContext(identity=AgentIdentity(17, True)).require_original_incarnation(17)
