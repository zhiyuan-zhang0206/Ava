"""Settlement edges of one managed exec that the real-subprocess tests do not reach."""

import asyncio
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from agent.graph.exec._owned_run import _OwnedRun


def _run(**fields: Any) -> _OwnedRun:
    run = object.__new__(_OwnedRun)
    defaults: dict[str, Any] = {
        "request_id": uuid4(),
        "request": "req",
        "scope": None,
        "proc": MagicMock(),
        "ready": MagicMock(),
        "reader": MagicMock(),
        "settled": False,
        "attached": True,
        "registration": None,
        "completion": None,
        "bound": time.monotonic() + 5,
        "_tasks": set(),
        "_errors": [],
    }
    for name, value in {**defaults, **fields}.items():
        setattr(run, name, value)
    return run


async def test_unsettled_attached_owner_is_retained_by_the_hosted_scope() -> None:
    run = _run(scope=SimpleNamespace(completions=set()))
    receipt = MagicMock()
    run.settle_attached_owner = AsyncMock(return_value=receipt)  # type: ignore[method-assign]

    async def completion() -> Any:
        return await run.settle_attached_owner()

    task = asyncio.ensure_future(completion())
    run.attached_completion = lambda: task  # type: ignore[assignment]

    assert run.needs_hand_off() is True
    await run.finish_owner()
    run.settle_attached_owner.assert_awaited_once()


async def test_owner_that_cannot_settle_raises_its_original_failure() -> None:
    run = _run(scope=SimpleNamespace(completions=set()))
    failure = RuntimeError("owner still open")

    async def failing() -> Any:
        raise failure

    task = asyncio.ensure_future(failing())
    run.attached_completion = lambda: task  # type: ignore[assignment]

    with pytest.raises(RuntimeError) as observed:
        await run.finish_owner()
    assert observed.value is failure


async def test_settled_unhosted_or_unattached_owner_is_not_handed_off() -> None:
    scope = SimpleNamespace(completions=set())
    assert _run(scope=scope, settled=True).needs_hand_off() is False
    assert _run(scope=None).needs_hand_off() is False
    assert _run(scope=scope, attached=False).needs_hand_off() is False


async def test_cancelled_registration_that_failed_settles_the_unpermitted_owner() -> None:
    from base.agents.incarnation.resources import ResourceEvidenceError

    scope = MagicMock()
    run = _run(scope=scope, attached=False)

    async def failed() -> None:
        raise ResourceEvidenceError("registration refused")

    registration = asyncio.ensure_future(failed())
    run.settle_unpermitted_owner = AsyncMock()  # type: ignore[method-assign]
    original = asyncio.CancelledError()

    await run.settle_registration_after_cancel(registration, original)

    assert run.settled is True and run.attached is False
    scope.complete.assert_called_once_with(run.request, None)
    assert not getattr(original, "__notes__", [])


async def test_unresolved_unpermitted_owner_leaves_a_note_and_stays_unsettled() -> None:
    from base.agents.incarnation.resources import ResourceEvidenceError

    scope = MagicMock()
    run = _run(scope=scope, attached=False)

    async def failed() -> None:
        raise ResourceEvidenceError("registration refused")

    registration = asyncio.ensure_future(failed())
    run.settle_unpermitted_owner = AsyncMock(side_effect=RuntimeError("still open"))  # type: ignore[method-assign]
    original = asyncio.CancelledError()

    await run.settle_registration_after_cancel(registration, original)

    assert run.settled is False
    scope.complete.assert_not_called()
    assert any("still open" in note for note in original.__notes__)
