"""The original hosted child Task stays inside its service's bounded join."""

import asyncio

import pytest

from base.native_process.turn_identity import HostedServiceResources, HostedTurnResources


async def test_join_retains_the_same_unfinished_child_without_cancelling_it() -> None:
    service = HostedServiceResources()
    scope = await service.turn()
    release = asyncio.Event()
    child = asyncio.create_task(release.wait(), name="original-hosted-child")
    service.retain_task(scope, child)
    try:
        with pytest.raises(TimeoutError, match="original-hosted-child"):
            await service.aclose(deadline=asyncio.get_running_loop().time())
        assert child in service._pending
        assert not child.done()
        assert not child.cancelling()
        assert not service.joined
    finally:
        release.set()
        await child
        await service.aclose()
    assert service.joined


async def test_original_error_is_visible_and_joined_without_cancelling_another_turn() -> None:
    service = HostedServiceResources()
    scope, sibling_scope = await service.turn(), await service.turn()
    error = ValueError("original hosted child failure")
    release = asyncio.Event()

    async def fail() -> None:
        raise error

    child = asyncio.create_task(fail(), name="failed-original-child")
    sibling = asyncio.create_task(release.wait(), name="unrelated-turn-child")
    service.retain_task(scope, child)
    service.retain_task(sibling_scope, sibling)
    try:
        await asyncio.wait({child}, timeout=1)
        await asyncio.sleep(0)
        assert service.failures == [(scope, error)]
        assert not sibling.done()
        assert not sibling.cancelling()
        with pytest.raises(ValueError) as observed:
            child.result()
        assert observed.value is error
    finally:
        release.set()
        await sibling
    with pytest.raises(ValueError) as joined:
        await service.aclose()
    assert joined.value is error
    assert service.joined


async def test_retained_turn_can_register_settlement_after_service_stop_started() -> None:
    service = HostedServiceResources()
    scope = await service.turn()
    turn_release, child_release, created = asyncio.Event(), asyncio.Event(), asyncio.Event()
    child: asyncio.Task[bool] | None = None

    async def turn() -> None:
        nonlocal child
        with service.hold_turn(asyncio.current_task()):
            await turn_release.wait()
            child = asyncio.create_task(child_release.wait(), name="original-settlement")
            service.retain_task(scope, child)
            created.set()

    root = asyncio.create_task(turn())
    await asyncio.sleep(0)
    joining = asyncio.create_task(service.aclose())
    try:
        await asyncio.sleep(0)
        turn_release.set()
        await created.wait()
        await root
        await asyncio.sleep(0)
        assert child is not None
        assert child in service._pending
        assert not joining.done()
    finally:
        turn_release.set()
        child_release.set()
        await root
        await joining
    assert service.joined


async def test_foreign_scope_cannot_replace_original_child_ownership() -> None:
    service, foreign = HostedServiceResources(), HostedServiceResources()
    scope, other = await service.turn(), await foreign.turn()
    release = asyncio.Event()
    child = asyncio.create_task(release.wait())
    service.retain_task(scope, child)
    try:
        with pytest.raises(RuntimeError, match="original service scope"):
            foreign.retain_task(scope, child)
        with pytest.raises(RuntimeError, match="original service scope"):
            service.retain_task(other, child)
        assert child in service._pending
        assert child not in foreign._pending
    finally:
        release.set()
        await child
        await service.aclose()
        await foreign.aclose()


async def test_explicit_scope_child_is_not_joined_without_entering_the_watch_group() -> None:
    service = HostedServiceResources()
    scope = HostedTurnResources(service=service)
    release = asyncio.Event()
    child = asyncio.create_task(release.wait(), name="explicit-scope-original")
    service.retain_task(scope, child)
    try:
        assert not service.joined
        with pytest.raises(TimeoutError, match="explicit-scope-original"):
            await service.aclose(deadline=asyncio.get_running_loop().time())
        assert not service.joined
        assert child in service._pending
    finally:
        release.set()
        await child
        await service.aclose()
    assert service.joined
    with pytest.raises(RuntimeError, match="already joined"):
        service.retain_task(scope, child)
