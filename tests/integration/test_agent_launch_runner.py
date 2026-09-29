"""The versioned launch operation is repeatable and never inserts a prompt."""

from __future__ import annotations

import pytest

from ops import lifecycle
from ops.lifecycle import launch
from ops.rpc_schemas import LaunchAgentRequest


@pytest.mark.asyncio
async def test_new_launch_attempt_is_repeatable_without_prompt_insertion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from uuid import uuid4

    stub_pool = object()

    validated: list[int] = []
    inserted: list[int] = []
    wakes: list[tuple[int, str]] = []

    def _validate(_pool: object, request: LaunchAgentRequest) -> None:
        validated.append(request.agent_id)

    def _insert(*_args: object) -> None:
        inserted.append(1)

    def _wake(agent_id: int, payload: str) -> None:
        wakes.append((agent_id, payload))

    monkeypatch.setattr(launch, "_validate_launch_row", _validate)
    monkeypatch.setattr(launch, "_insert_prompt_blocking", _insert)
    monkeypatch.setattr(launch, "publish_inbound_wake", _wake)
    body = LaunchAgentRequest(agent_id=7, launch_attempt_id=uuid4())
    await lifecycle.launch_agent_op(body, stub_pool)  # type: ignore[arg-type]
    await lifecycle.launch_agent_op(body, stub_pool)  # type: ignore[arg-type]
    assert validated == [7, 7]
    assert inserted == []
    assert wakes == [(7, "0"), (7, "0")]
