"""Model and subscription owners preserve errors through bounded invocation return."""

import asyncio
from typing import Any, cast

import pytest
from psycopg_pool import AsyncConnectionPool

from agent.graph import interrupt
from agent.graph.interrupt import ModelInterruptedError, interruptible_model
from base.native_process.turn_identity import HostedServiceResources, HostedTurnResources


async def test_original_provider_failure_reaches_caller_without_exception_group() -> None:
    original = ValueError("provider failed")

    async def provider() -> str:
        raise original

    with pytest.raises(ValueError) as caught:
        await interruptible_model(provider(), asyncio.Event())
    assert caught.value is original


async def test_interrupt_waits_for_provider_cleanup_and_exposes_unknown_cleanup_error() -> None:
    event, entered, unwound = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = RuntimeError("provider cleanup failed")

    async def provider() -> str:
        entered.set()
        try:
            await asyncio.Future()
        finally:
            await asyncio.sleep(0)
            unwound.set()
            raise original

    async def interrupt() -> None:
        await entered.wait()
        event.set()

    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(interrupt())
        with pytest.raises(RuntimeError) as caught:
            await interruptible_model(provider(), event)
    assert caught.value is original
    assert unwound.is_set()


async def test_interrupt_returns_only_after_provider_unwinds() -> None:
    event, entered, unwound = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def provider() -> str:
        entered.set()
        try:
            await asyncio.Future()
        finally:
            await asyncio.sleep(0)
            unwound.set()
        raise AssertionError("unreachable")

    async def interrupt() -> None:
        await entered.wait()
        event.set()

    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(interrupt())
        with pytest.raises(ModelInterruptedError):
            await interruptible_model(provider(), event)
    assert unwound.is_set()


async def test_provider_value_that_is_an_exception_is_still_a_value() -> None:
    value = ValueError("a legitimate provider value")

    async def provider() -> ValueError:
        return value

    assert await interruptible_model(provider(), asyncio.Event()) is value


def _pool() -> AsyncConnectionPool:
    # These tests fail or replace the polling boundary before it uses the pool.
    return cast(AsyncConnectionPool, object())


@pytest.mark.parametrize("missing_scope", [True, False])
async def test_database_subscription_refuses_an_unowned_watcher(missing_scope: bool) -> None:
    scope = None if missing_scope else HostedTurnResources()
    with pytest.raises(RuntimeError, match="service"):
        async with interrupt.subscribe_interrupt(
            _pool(), 1, incarnation=None, work=None, resources=scope
        ):
            pytest.fail("an unowned watcher must not start")


async def test_inflight_unknown_reaches_caller_with_original_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = HostedServiceResources()
    scope = await service.turn()
    original = ValueError("invalid watcher boundary")

    async def fail(*_args: object, **_kwargs: object) -> None:
        raise original

    monkeypatch.setattr(interrupt, "_watch_for_interrupt", fail)
    with pytest.raises(ValueError) as observed:
        async with interrupt.subscribe_interrupt(
            _pool(), 1, incarnation=None, work=None, resources=scope
        ) as event:
            await event.wait()
    assert observed.value is original
    assert not service.failures
    await service.aclose()


async def test_primary_and_watcher_cleanup_unknown_both_survive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = HostedServiceResources()
    scope = await service.turn()
    entered = asyncio.Event()
    primary, secondary = RuntimeError("body failed"), ValueError("watcher cleanup failed")

    async def fail_cleanup(*_args: object, **_kwargs: object) -> None:
        entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            raise secondary from None

    monkeypatch.setattr(interrupt, "_watch_for_interrupt", fail_cleanup)
    with pytest.raises(RuntimeError) as observed:
        async with interrupt.subscribe_interrupt(
            _pool(), 1, incarnation=None, work=None, resources=scope
        ):
            await entered.wait()
            raise primary
    assert observed.value is primary
    cause = cast(ExceptionGroup[Exception], primary.__cause__)
    assert isinstance(cause, ExceptionGroup)
    assert cause.exceptions == (secondary,)
    assert not service.failures
    await service.aclose()


async def test_late_unknown_is_visible_before_service_join_without_sibling_cancel(
    monkeypatch: pytest.MonkeyPatch, loguru_records: list[dict[str, Any]]
) -> None:
    service = HostedServiceResources()
    scope = await service.turn()
    entered, release, sibling_alive = asyncio.Event(), asyncio.Event(), asyncio.Event()
    failure = RuntimeError("late poll failed")

    async def late_failure(*_args: object, **_kwargs: object) -> None:
        entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            await release.wait()
            raise failure from None

    async def other_turn() -> None:
        await release.wait()
        await asyncio.sleep(0)
        sibling_alive.set()

    monkeypatch.setattr(interrupt, "_watch_for_interrupt", late_failure)
    monkeypatch.setattr(interrupt, "_WATCHER_EXIT_TIMEOUT_S", 0.02)
    async with interrupt.subscribe_interrupt(
        _pool(), 1, incarnation=None, work=None, resources=scope
    ) as old_event:
        await entered.wait()
    assert not old_event.is_set()
    assert not service.failures
    service.complete_later(await service.turn(), other_turn(), name="other-agent")
    release.set()
    await sibling_alive.wait()
    assert service.failures == [(scope, failure)]
    assert not old_event.is_set()
    assert any("retained until service stop/join" in record["message"] for record in loguru_records)
    with pytest.raises(RuntimeError) as observed:
        await service.aclose()
    assert observed.value is failure


async def test_repeated_cancel_during_bounded_exit_still_hands_off_actual_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = HostedServiceResources()
    scope = await service.turn()
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    failure = ValueError("after repeated caller cancel")

    async def late_failure(*_args: object, **_kwargs: object) -> None:
        entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
            raise failure from None

    async def invoke() -> None:
        async with interrupt.subscribe_interrupt(
            _pool(), 1, incarnation=None, work=None, resources=scope
        ):
            await entered.wait()

    monkeypatch.setattr(interrupt, "_watch_for_interrupt", late_failure)
    monkeypatch.setattr(interrupt, "_WATCHER_EXIT_TIMEOUT_S", 0.03)
    caller = asyncio.create_task(invoke())
    await cancelled.wait()
    caller.cancel()
    await asyncio.sleep(0)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(caller, 0.5)
    release.set()
    with pytest.raises(ValueError) as observed:
        await service.aclose()
    assert observed.value is failure
