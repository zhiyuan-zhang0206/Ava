"""A hosted agent's chat model is built with the agent's own tuning pins."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest

from base.host.env.agent_slices import AgentSlices, ModelOverrides
from base.lm.catalog import ModelCatalog
from base.packages.composer_commands import CommandCatalog
from services.agent_runner.agent_host import host as host_module
from services.agent_runner.agent_host import runtime as runtime_module
from services.agent_runner.agent_host.host import AgentHost
from services.agent_runner.agent_host.tests.host_policy import configured_machine, configured_policy


@pytest.mark.parametrize("pinned", [True, False])
async def test_the_runtime_is_built_with_the_agents_overrides(
    monkeypatch: pytest.MonkeyPatch, pinned: bool, *, model_catalog: ModelCatalog
) -> None:
    built: list[tuple[str, ModelOverrides]] = []

    async def _boot(
        _agent_id: int,
        llm_model: str,
        overrides: ModelOverrides,
        *,
        catalog: ModelCatalog,
        llm_override: str,
    ) -> tuple[object, None]:
        assert catalog is model_catalog
        assert isinstance(llm_override, str)
        built.append((llm_model, overrides))
        return object(), None

    monkeypatch.setattr(host_module, "consume_claimed_inbounds", AsyncMock())
    monkeypatch.setattr(runtime_module, "boot_agent_scope", _boot)
    host = AgentHost(
        policy=configured_policy(),
        pool=Mock(),
        checkpointer=Mock(),
        graph=Mock(),
        bus=Mock(),
        db=Mock(),
        catalog=model_catalog,
        machine=configured_machine(),
        commands=CommandCatalog(skills=list),
    )
    pins = {"reasoning_effort": "low", "claude_thinking_budget_tokens": 777} if pinned else {}
    slices = AgentSlices.resolve(
        {"llm_model": "pinned-model", **pins}, default_reader=configured_policy().default_reader
    )

    await host._build_runtime(1, "fingerprint", slices, incarnation=None)

    assert [model for model, _ in built] == ["pinned-model"]
    overrides = built[0][1]
    assert overrides is slices.overrides
    if pinned:
        assert overrides.reasoning_effort == "low"
        assert overrides.claude_thinking_budget_tokens == 777
    else:
        assert (
            overrides
            == AgentSlices.resolve(default_reader=configured_policy().default_reader).overrides
        )


async def test_runtime_build_does_not_repair_a_dangling_history(
    monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
) -> None:
    """Cold runtime construction never reads or rewrites the agent's checkpoint history.

    A dangling tool_use is repaired by the before_llm hook (`agent/hooks/repair.py`)
    on the first LLM call, not at startup.
    """
    touched: list[str] = []

    class _RecordingGraph:
        async def aget_state(self, *_args: object, **_kwargs: object) -> object:
            touched.append("aget_state")
            raise AssertionError("startup must not read graph state")

        async def aupdate_state(self, *_args: object, **_kwargs: object) -> None:
            touched.append("aupdate_state")
            raise AssertionError("startup must not rewrite graph state")

    async def _boot(*_args: object, **_kwargs: object) -> tuple[object, None]:
        return object(), None

    monkeypatch.setattr(host_module, "consume_claimed_inbounds", AsyncMock())
    monkeypatch.setattr(runtime_module, "boot_agent_scope", _boot)
    host = AgentHost(
        policy=configured_policy(),
        pool=Mock(),
        checkpointer=Mock(),
        graph=_RecordingGraph(),  # type: ignore[arg-type]
        bus=Mock(),
        db=Mock(),
        catalog=model_catalog,
        machine=configured_machine(),
        commands=CommandCatalog(skills=list),
    )
    slices = AgentSlices.resolve({}, default_reader=configured_policy().default_reader)

    await host._build_runtime(1, "fingerprint", slices, incarnation=None)

    assert touched == []
