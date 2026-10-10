"""The actual run_turn root preserves children, native barriers and primary errors."""

import asyncio
from collections.abc import Awaitable

import pytest

from services.agent_runner.agent_host.invocation.compact import source as compact_source
from services.agent_runner.agent_host.tests.test_agent_host import _Build, _Row
from services.agent_runner.agent_host.tests.test_agent_host import host_plugin as host_plugin
from services.agent_runner.agent_host.tests.test_agent_host import wired as wired


async def test_business_error_remains_primary_when_settlement_also_fails(
    wired: _Build, monkeypatch: pytest.MonkeyPatch
) -> None:
    host, _, _ = wired({7: _Row()})
    primary = ValueError("original hosted business failure")
    cleanup = RuntimeError("original hosted settlement failure")

    async def work(_agent: int, **_kwargs: object) -> None:
        raise primary

    async def settle(force: Awaitable[bool], *_args: object, **_kwargs: object) -> None:
        await force
        raise cleanup

    monkeypatch.setattr(host, "_run_turn", work)
    monkeypatch.setattr(compact_source, "finish_force_and_compact", settle)
    with pytest.raises(ValueError) as observed:
        await host.run_turn(7)
    assert observed.value is primary
    assert any(repr(cleanup) in note for note in primary.__notes__)
    with pytest.raises(ExceptionGroup) as joined:
        await host._resource_service.aclose()
    assert joined.value.exceptions == (primary, cleanup)


async def test_root_cancellation_does_not_cancel_or_disown_work_and_settlement(
    wired: _Build, monkeypatch: pytest.MonkeyPatch
) -> None:
    host, _, _ = wired({7: _Row()})
    work_entered, work_release = asyncio.Event(), asyncio.Event()
    settle_entered, settle_release = asyncio.Event(), asyncio.Event()
    actual_work: asyncio.Task[object] | None = None
    actual_settle: asyncio.Task[object] | None = None

    async def work(_agent: int, **_kwargs: object) -> None:
        nonlocal actual_work
        actual_work = asyncio.current_task()
        work_entered.set()
        await work_release.wait()

    async def settle(force: Awaitable[bool], *_args: object, **_kwargs: object) -> None:
        nonlocal actual_settle
        await force
        actual_settle = asyncio.current_task()
        settle_entered.set()
        await settle_release.wait()

    monkeypatch.setattr(host, "_run_turn", work)
    monkeypatch.setattr(compact_source, "finish_force_and_compact", settle)
    root = asyncio.create_task(host.run_turn(7))
    try:
        await work_entered.wait()
        assert actual_work is not None and actual_work in host._resource_service._pending
        root.cancel()
        await asyncio.sleep(0)
        assert not actual_work.cancelling()
        assert not root.done()
        work_release.set()
        await settle_entered.wait()
        assert actual_settle is not None and actual_settle in host._resource_service._pending
        root.cancel()
        with pytest.raises(TimeoutError, match="unfinished"):
            await host._resource_service.aclose(deadline=asyncio.get_running_loop().time())
        assert not host.resources_joined
        assert not actual_settle.cancelling()
        assert actual_settle in host._resource_service._pending
        assert not root.done()
    finally:
        root.cancel()
        work_release.set()
        settle_release.set()
        with pytest.raises(asyncio.CancelledError):
            await root
        await host._resource_service.aclose()
    assert host.resources_joined
