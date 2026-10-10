"""The bounded model race observes both tasks without replacing their errors."""

import asyncio

import pytest

from agent.graph.interrupt import ModelInterruptedError, interruptible_model


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
