"""A hosted agent's chat model is built with the agent's own tuning pins."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest

from base.host.env.agent_slices import AgentSlices, ModelOverrides
from services.agent_runner.agent_host import host as host_module
from services.agent_runner.agent_host import runtime as runtime_module
from services.agent_runner.agent_host.host import AgentHost


@pytest.mark.parametrize("pinned", [True, False])
async def test_the_runtime_is_built_with_the_agents_overrides(
    monkeypatch: pytest.MonkeyPatch, pinned: bool
) -> None:
    built: list[tuple[str, ModelOverrides]] = []

    async def _boot(
        _agent_id: int, llm_model: str, overrides: ModelOverrides
    ) -> tuple[object, None]:
        built.append((llm_model, overrides))
        return object(), None

    monkeypatch.setattr(host_module, "reconcile_claimed_inbounds_at_startup", AsyncMock())
    monkeypatch.setattr(host_module, "repair_dangling_tool_use_at_startup", AsyncMock())
    monkeypatch.setattr(runtime_module, "boot_agent_scope", _boot)
    host = AgentHost(pool=Mock(), checkpointer=Mock(), graph=Mock(), bus=Mock(), db=Mock())
    pins = {"reasoning_effort": "low", "claude_thinking_budget_tokens": 777} if pinned else {}
    slices = AgentSlices.resolve({"llm_model": "pinned-model", **pins})

    await host._build_runtime(1, "fingerprint", slices, incarnation=None)

    assert [model for model, _ in built] == ["pinned-model"]
    overrides = built[0][1]
    assert overrides is slices.overrides
    if pinned:
        assert overrides.reasoning_effort == "low"
        assert overrides.claude_thinking_budget_tokens == 777
    else:
        assert overrides == AgentSlices.resolve().overrides
