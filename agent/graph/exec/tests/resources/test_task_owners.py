"""Actual exec owner handles survive finite observation and retain original errors."""

import asyncio
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.graph.exec import _process
from agent.graph.exec._owned_run import _OwnedRun
from agent.graph.exec._subprocess import _finish_failed_run
from base.native_process.turn_identity import HostedServiceResources


async def test_native_stop_retains_same_tasks_without_cancelling_reap() -> None:
    release, entered = threading.Event(), threading.Event()
    exited = asyncio.Event()
    loop = asyncio.get_running_loop()
    service = HostedServiceResources()
    scope = await service.turn()
    request = Path("native-stuck-request")

    class Proc:
        pid = 8754

        def wait(self, timeout: float) -> int:
            assert release.is_set()
            return 0

    class Domain:
        proc = Proc()

        def close_confirmed(self, deadline: float) -> None:
            entered.set()
            assert release.wait(timeout=max(0, deadline - time.monotonic()))
            loop.call_soon_threadsafe(exited.set)

    reader = MagicMock(closed=True, finish=AsyncMock())
    owner = _process.DomainCloseOwner(Domain(), asyncio.create_task(exited.wait()))  # type: ignore[arg-type]
    scope.unresolved[request] = owner
    reap = owner.start_reap()
    tail = owner.start_reader_join(reap, reader)

    async def finish() -> None:
        await owner.finish_later()
        scope.complete(request, owner)

    try:
        unfinished = await owner.stop(0.02)
        assert entered.is_set()
        assert owner.task in unfinished and reap in unfinished and tail in unfinished
        assert owner.start_reap() is reap
        assert owner.start_reader_join(reap, reader) is tail
        assert all(task.cancelling() == 0 for task in unfinished)
        scope.require_service().complete_later(scope, finish(), name="same-native-owner")
        with pytest.raises(TimeoutError, match="remains unfinished"):
            await service.aclose(deadline=loop.time() + 0.02)
        assert not service.joined and scope.unresolved[request] is owner
        assert all(task in owner._tasks for task in unfinished)
    finally:
        release.set()
        await asyncio.gather(*owner._tasks, return_exceptions=True)
        await service.aclose(deadline=loop.time() + 1)
    assert not scope.unresolved and service.joined


async def test_native_unknown_keeps_original_identity_and_body_primary() -> None:
    original = OSError("native closure failed")
    body = ValueError("business failed")
    proc = MagicMock(pid=8755)
    domain = MagicMock(proc=proc)
    domain.close_confirmed.side_effect = original

    async def observe() -> None:
        await asyncio.Event().wait()

    root = asyncio.create_task(observe())
    owner = _process.DomainCloseOwner(domain, root)
    reap = owner.start_reap()
    failures = await _process.finish_teardown_despite_cancellation(root, reap, owner, None)
    assert failures[0].error is original
    assert owner._errors[0] is original
    _process.annotate_original_failure(body, failures)
    assert "native closure failed" in body.__notes__[0]
    with pytest.raises(OSError) as observed:
        await owner.finish_later()
    assert observed.value is original
    proc.wait.assert_not_called()


def _managed(**fields: Any) -> _OwnedRun:
    owner = object.__new__(_OwnedRun)
    defaults: dict[str, Any] = {
        "request": Path("pending-managed-request"),
        "request_id": "original",
        "scope": None,
        "proc": MagicMock(),
        "ready": MagicMock(),
        "reader": MagicMock(),
        "registration": None,
        "completion": None,
        "attached": False,
        "settled": False,
        "bound": time.monotonic() + 0.02,
        "_tasks": set(),
        "_errors": [],
    }
    for name, value in {**defaults, **fields}.items():
        setattr(owner, name, value)
    return owner


async def test_pending_registration_is_handed_to_original_service_after_bound() -> None:
    service = HostedServiceResources()
    scope = await service.turn()
    owner = _managed(scope=scope)
    scope.unresolved[owner.request] = None
    release = asyncio.Event()

    async def register() -> None:
        await release.wait()

    registration = asyncio.create_task(register(), name="same-registration")
    owner.registration = registration
    owner._register(registration)
    owner.settle_attached_owner = AsyncMock(return_value=MagicMock())  # type: ignore[method-assign]
    original = asyncio.CancelledError("original cancel")
    try:
        await owner.on_cancelled(original)
        assert owner.needs_hand_off()
        assert not registration.done() and not registration.cancelling()
        assert any("remains unfinished" in note for note in original.__notes__)
        assert await owner.stop(0) == (registration,)
        scope.require_service().complete_later(
            scope, owner.finish_owner(), name="same-managed-owner"
        )
        with pytest.raises(TimeoutError):
            await service.aclose(deadline=asyncio.get_running_loop().time() + 0.01)
        assert not service.joined and scope.unresolved[owner.request] is None
    finally:
        release.set()
        await asyncio.gather(registration)
        await asyncio.gather(*scope.completions)
        await service.aclose(deadline=asyncio.get_running_loop().time() + 1)
    assert owner.registration is registration and owner.attached
    owner.settle_attached_owner.assert_awaited_once()


async def test_late_registration_unknown_is_visible_without_cancelling_another_turn() -> None:
    service = HostedServiceResources()
    scope = await service.turn()
    owner = _managed(scope=scope)
    scope.unresolved[owner.request] = None
    original = RuntimeError("late registration unknown")
    release = asyncio.Event()
    other_turn = asyncio.create_task(asyncio.Event().wait())

    async def register() -> None:
        await release.wait()
        raise original

    registration = asyncio.create_task(register())
    owner.registration = registration
    owner._register(registration)
    service.complete_later(scope, owner.finish_owner(), name="pending-registration")
    try:
        release.set()
        await asyncio.gather(*scope.completions)
        assert service.failures == [(scope, original)]
        assert owner._errors == [original]
        assert not other_turn.done() and not other_turn.cancelling()
        assert scope.unresolved[owner.request] is None
        with pytest.raises(RuntimeError) as observed:
            await service.aclose()
        assert observed.value is original
    finally:
        other_turn.cancel()
        await asyncio.gather(other_turn, return_exceptions=True)


async def test_pending_registration_completion_preserves_replacement_scope_entry() -> None:
    service = HostedServiceResources()
    scope = await service.turn()
    owner = _managed(scope=scope)
    replacement = object()
    scope.unresolved[owner.request] = replacement
    registration = asyncio.create_task(asyncio.sleep(0))
    owner.registration = registration
    owner._register(registration)

    async def receipt() -> object:
        scope.complete(owner.request, owner.ready)
        return object()

    owner.settle_attached_owner = AsyncMock(side_effect=receipt)  # type: ignore[method-assign]
    await owner.finish_owner()
    assert scope.unresolved[owner.request] is replacement
    assert not scope.changed.is_set()
    await service.aclose()


async def test_native_owner_refuses_replacement_reap_or_reader_handles() -> None:
    domain = MagicMock(proc=MagicMock(pid=8756))
    root = asyncio.create_task(asyncio.sleep(0))
    owner = _process.DomainCloseOwner(domain, root)
    reap = owner.start_reap()
    other = asyncio.create_task(asyncio.sleep(0, result=0))
    reader = MagicMock(closed=True, finish=AsyncMock())
    try:
        owner.start_reader_join(reap, reader)
        with pytest.raises(RuntimeError, match="another resource owner"):
            owner.start_reader_join(other, reader)
        with pytest.raises(RuntimeError, match="another output pipe"):
            owner.start_reader_join(reap, MagicMock())
        with pytest.raises(RuntimeError, match="another resource owner"):
            owner.start_teardown(other, owner.reader_join_task)
    finally:
        await owner.stop(1)
        await other


async def test_unknown_stop_failure_cannot_be_erased_by_successful_stage_tasks() -> None:
    domain = MagicMock(proc=MagicMock(pid=8757))
    domain.proc.wait.return_value = 0
    root = asyncio.create_task(asyncio.sleep(0))
    owner = _process.DomainCloseOwner(domain, root)
    reap = owner.start_reap()
    reader = MagicMock(closed=True, finish=AsyncMock())
    tail = owner.start_reader_join(reap, reader)
    await asyncio.gather(root, owner.task, reap, tail)
    assert not owner._errors
    sentinel = RuntimeError("unknown stop observation bug")
    owner.stop = AsyncMock(side_effect=sentinel)  # type: ignore[method-assign]

    failures = await _process.settle_resources(root, reap, owner, tail, request_stop=True)
    assert len(failures) == 1 and failures[0].error is sentinel
    body = ValueError("original business failure")
    settled = await _finish_failed_run(body, root, reap, owner, tail, reader, resources=None)
    assert not settled
    assert "unknown stop observation bug" in body.__notes__[0]
    assert owner.teardown_task is not None
    receipt = owner.teardown_task.result()
    assert len(receipt) == 1 and receipt[0].error is sentinel
