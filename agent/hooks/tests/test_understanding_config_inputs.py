"""Understanding producers read the owning root only at their existing gates."""

from typing import Any
from unittest.mock import MagicMock

from langchain_core.messages import AIMessage, HumanMessage

from agent.hooks.understanding_chunks import await_snapshot, due_chunk_update
from agent.state_channels import CompactState
from base.config import ConfigBoot
from base.host.env.agent_slices import ModelOverrides
from base.lm.plugin_providers import build_model_catalog


async def test_two_roots_keep_live_understanding_gates_independent() -> None:
    first, second = ConfigBoot(), ConfigBoot()
    first.set_field("understanding_enabled", False)
    second.set_field("understanding_enabled", True)
    final = AIMessage(
        content="reply",
        usage_metadata={
            "input_tokens": 10,
            "output_tokens": 1,
            "total_tokens": 11,
        },
    )
    reads: list[str] = []

    def reader(owner: ConfigBoot, field: str) -> Any:
        reads.append(field)
        return getattr(owner.view.agent, field)

    async def update(owner: ConfigBoot, pool: Any):
        return await due_chunk_update(
            CompactState(),
            [HumanMessage(content="request")],
            final,
            pool=pool,
            agent_id=1,
            model="m",
            overrides=ModelOverrides.from_pins({}),
            catalog=build_model_catalog(),
            read_agent=lambda field: reader(owner, field),
        )

    assert await update(first, MagicMock()) == {}
    assert (await update(second, MagicMock()))["compact"].understanding_cut_tokens == 10
    first.set_field("understanding_enabled", True)
    second.set_field("understanding_enabled", False)
    assert (await update(first, MagicMock()))["compact"].understanding_cut_tokens == 10
    assert await update(second, MagicMock()) == {}
    assert reads == ["understanding_enabled"] * 4
    reads.clear()
    assert await update(first, None) == {}
    await await_snapshot(None, None, 1, read_agent=lambda field: reader(first, field))
    assert reads == []
