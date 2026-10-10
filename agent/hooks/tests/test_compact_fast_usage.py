"""Compaction accounts for the selected service rather than the wire model."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agent.hooks.compact import generate_summary
from base.config import settings
from base.host.env.agent_slices import AgentSlices
from base.lm.plugin_providers import build_model_catalog


@pytest.mark.asyncio
async def test_compaction_preserves_fast_accounting_id() -> None:
    slices = AgentSlices.resolve(
        {"llm_model": "gpt-6.1-sol-fast"},
        default_reader=lambda domain, field: getattr(getattr(settings, domain), field),
    )
    llm = MagicMock()
    llm.model_name = "gpt-6.1-sol"
    response = AIMessage(
        content="summary",
        response_metadata={"service_tier": "fast"},
        usage_metadata={"input_tokens": 100, "output_tokens": 10, "total_tokens": 110},
    )
    with (
        patch(
            "agent.hooks.compact.ainvoke_tool_call",
            new=AsyncMock(return_value=response),
        ),
        patch("base.lm.usage.log_usage_from_message") as account,
    ):
        assert (
            await generate_summary(
                [HumanMessage(content="history")], llm, slices, catalog=build_model_catalog()
            )
            == "summary"
        )
    assert account.call_args.kwargs["model"] == "gpt-6.1-sol-fast"
