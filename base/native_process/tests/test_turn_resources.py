"""Explicit invocation contexts retain their original resource and admission owners."""

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from base.agents.context import AvaContext
from base.agents.context.identity import AgentIdentity
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.native_process.turn_identity import (
    HostedServiceResources,
    HostedTurnResources,
    hosted_resources_settled,
)


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


async def test_late_failure_keeps_original_scope_without_cancelling_other_turns(
    loguru_records: list[dict[str, Any]],
) -> None:
    service = HostedServiceResources()
    first, second = await service.turn(), await service.turn()
    request, domain = Path("original-request"), object()
    first.unresolved[request] = domain
    failure = RuntimeError("late original completion failed")
    release, sibling_finished = asyncio.Event(), asyncio.Event()

    async def fail_original() -> None:
        await release.wait()
        raise failure

    async def finish_sibling() -> None:
        await release.wait()
        await asyncio.sleep(0)
        sibling_finished.set()

    service.complete_later(first, fail_original(), name="original-completion")
    service.complete_later(second, finish_sibling(), name="next-turn-completion")
    release.set()
    await sibling_finished.wait()
    assert service.failures == [(first, failure)]
    assert first.unresolved[request] is domain
    assert not hosted_resources_settled(first)
    assert any("retained until service stop/join" in record["message"] for record in loguru_records)
    with pytest.raises(RuntimeError) as observed:
        await service.aclose()
    assert observed.value is failure
    assert first.unresolved[request] is domain


async def test_service_join_waits_for_the_actual_completion() -> None:
    service = HostedServiceResources()
    scope = await service.turn()
    request, domain = Path("request"), object()
    scope.unresolved[request] = domain
    release = asyncio.Event()

    async def complete_original() -> None:
        await release.wait()
        assert scope.complete(request, domain)

    service.complete_later(scope, complete_original(), name="actual-completion")
    joining = asyncio.create_task(service.aclose())
    await asyncio.sleep(0)
    assert not joining.done()
    assert scope.unresolved[request] is domain
    release.set()
    await joining
    assert hosted_resources_settled(scope)
    assert not scope.completions
    with pytest.raises(RuntimeError, match="already closed"):
        await service.turn()


async def test_service_join_preserves_every_late_unknown_error() -> None:
    service = HostedServiceResources()
    scope = await service.turn()
    first, second = RuntimeError("first"), ValueError("second")
    service.record_failure(scope, first, name="first")
    service.record_failure(scope, second, name="second")
    with pytest.raises(ExceptionGroup) as observed:
        await service.aclose()
    assert observed.value.exceptions == (first, second)


async def test_uncooperative_completion_returns_bounded_without_discharge() -> None:
    service = HostedServiceResources()
    scope = await service.turn()
    path, domain = Path("still-running"), object()
    scope.unresolved[path] = domain
    release = asyncio.Event()

    async def complete() -> None:
        await release.wait()
        scope.complete(path, domain)

    service.complete_later(scope, complete(), name="uncooperative-original")
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(TimeoutError, match="uncooperative-original"):
        await service.aclose(deadline=started + 0.02)
    assert loop.time() - started < 0.5
    assert not service.joined
    assert scope.unresolved[path] is domain
    assert len(scope.completions) == 1
    assert not next(iter(scope.completions)).done()
    release.set()
    await service.aclose()
    assert service.joined
    assert not scope.unresolved


async def test_join_tracks_completion_created_by_a_retained_turn_after_stop() -> None:
    service = HostedServiceResources()
    scope = await service.turn()
    turn_release, completion_release, created = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def completion() -> None:
        await completion_release.wait()

    async def turn() -> None:
        with service.hold_turn(asyncio.current_task()):
            await turn_release.wait()
            service.complete_later(scope, completion(), name="created-after-stop")
            created.set()

    root = asyncio.create_task(turn(), name="original-turn")
    await asyncio.sleep(0)
    joining = asyncio.create_task(service.aclose())
    turn_release.set()
    await created.wait()
    await root
    await asyncio.sleep(0)
    assert not joining.done()
    completion_release.set()
    await joining
    assert service.joined


async def test_unknown_and_join_timeout_preserve_both_results() -> None:
    service = HostedServiceResources()
    scope = await service.turn()
    failure = ValueError("original late unknown")
    service.record_failure(scope, failure, name="original")
    release = asyncio.Event()

    async def pending() -> None:
        await release.wait()

    service.complete_later(scope, pending(), name="pending")
    with pytest.raises(ExceptionGroup) as observed:
        await service.aclose(deadline=asyncio.get_running_loop().time())
    assert observed.value.exceptions[0] is failure
    assert isinstance(observed.value.exceptions[1], TimeoutError)
    assert not service.joined
    release.set()
    with pytest.raises(ValueError) as final:
        await service.aclose()
    assert final.value is failure
    assert service.joined


async def test_repeated_join_cancel_keeps_new_completion_owned() -> None:
    service = HostedServiceResources()
    scope = await service.turn()
    turn_release, completion_release = asyncio.Event(), asyncio.Event()

    async def completion() -> None:
        await completion_release.wait()

    async def turn() -> None:
        with service.hold_turn(asyncio.current_task()):
            await turn_release.wait()
            service.complete_later(scope, completion(), name="created-during-join-cancel")
            joining.cancel()

    root = asyncio.create_task(turn(), name="original-turn")
    await asyncio.sleep(0)
    joining = asyncio.create_task(service.aclose())
    await asyncio.sleep(0)
    turn_release.set()
    await root
    await asyncio.sleep(0)
    assert not joining.done()
    completion_release.set()
    with pytest.raises(asyncio.CancelledError):
        await joining
    assert service.joined
    assert not scope.completions


async def test_same_exception_from_distinct_scopes_keeps_both_original_owners() -> None:
    service = HostedServiceResources()
    first, second = await service.turn(), await service.turn()
    error = ValueError("shared exception instance")
    service.record_failure(first, error, name="first")
    service.record_failure(first, error, name="first again")
    service.record_failure(second, error, name="second")
    assert service.failures == [(first, error), (second, error)]
    with pytest.raises(ExceptionGroup) as observed:
        await service.aclose()
    assert observed.value.exceptions == (error, error)
