"""The stall guard retains its original invocation in the existing service scope."""

import asyncio
from unittest.mock import MagicMock

import pytest

from base.agents.context import AvaContext
from base.agents.context.identity import AgentIdentity
from base.native_process.turn_identity import HostedServiceResources
from services.agent_runner.agent_host.stall_guard import run_invocation_with_stall_guard


async def test_guard_registers_the_actual_invocation_and_preserves_its_result() -> None:
    service = HostedServiceResources()
    scope = await service.turn()
    ctx = AvaContext(identity=AgentIdentity(7, True), hosted_resources=scope)
    entered, release = asyncio.Event(), asyncio.Event()
    actual: asyncio.Task[object] | None = None
    result: dict[str, object] = {"completed": True}

    async def invoke(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal actual
        actual = asyncio.current_task()
        entered.set()
        await release.wait()
        return result

    graph = MagicMock()
    graph.ainvoke = invoke
    guard = asyncio.create_task(run_invocation_with_stall_guard(graph, 7, ctx, {}, {}))
    try:
        await entered.wait()
        assert actual is not None and actual in service._pending
        release.set()
        assert await guard is result
        assert actual.done()
        assert actual.result() is result
    finally:
        release.set()
        await guard
        await service.aclose()


async def test_external_cancel_retains_the_same_unknown_invocation_error() -> None:
    service = HostedServiceResources()
    scope = await service.turn()
    ctx = AvaContext(identity=AgentIdentity(7, True), hosted_resources=scope)
    entered = asyncio.Event()
    error = ValueError("original invocation cancellation failure")
    actual: asyncio.Task[object] | None = None

    async def invoke(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal actual
        actual = asyncio.current_task()
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise error from None
        return {}

    graph = MagicMock()
    graph.ainvoke = invoke
    guard = asyncio.create_task(run_invocation_with_stall_guard(graph, 7, ctx, {}, {}))
    await entered.wait()
    guard.cancel()
    with pytest.raises(asyncio.CancelledError):
        await guard
    await asyncio.sleep(0)
    assert actual is not None
    with pytest.raises(ValueError) as original:
        actual.result()
    assert original.value is error
    assert service.failures == [(scope, error)]
    with pytest.raises(ValueError) as joined:
        await service.aclose()
    assert joined.value is error


async def test_guard_requires_an_explicit_service_before_starting_invocation() -> None:
    graph = MagicMock()
    with pytest.raises(RuntimeError, match="original resource scope"):
        await run_invocation_with_stall_guard(graph, 7, AvaContext(), {}, {})
    graph.ainvoke.assert_not_called()
