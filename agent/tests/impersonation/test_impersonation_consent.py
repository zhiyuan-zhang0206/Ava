from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import HumanMessage

from agent import impersonation
from agent.state import BaseAgentState
from agent.tests.impersonation.test_impersonation import _session
from agent.tests.impersonation.test_impersonation import gate_ctx as gate_ctx
from agent.tests.impersonation.test_impersonation import relays as relays
from base.agents.context import MeivaContext
from base.agents.messages.kwargs import read_meiva_kwargs
from base.host.env.agent_slices import AgentSlices


@pytest.mark.parametrize("timestamps_enabled", [False, True])
async def test_consent_carries_actual_source_and_survives_compaction(
    monkeypatch: pytest.MonkeyPatch,
    gate_ctx: MeivaContext,
    timestamps_enabled: bool,
) -> None:
    gate_ctx = replace(
        gate_ctx,
        agent=AgentSlices.resolve(
            pins={"message_timestamps": timestamps_enabled},
            default_reader=gate_ctx.require_agent().read,
        ),
    )
    monkeypatch.setattr(
        impersonation, "native_status", AsyncMock(return_value=_session("requested"))
    )
    first = await impersonation.claim_gate(BaseAgentState(), 42, gate_ctx)
    assert first is not None
    assert first.goto == "before_llm"
    message = cast(HumanMessage, cast(dict[str, Any], first.update)["messages"][0])
    assert message.id == "impersonation-request:lease-1:1"
    content = cast(str, message.content)
    assert "External agent" in content and "codex" in content
    assert (" [" in content.split("\n\n", 1)[0]) is timestamps_enabled
    assert "meiva.impersonation.accept('lease-1', start_message=" in content
    compacted = BaseAgentState(impersonation_request_id="lease-1:1")
    assert await impersonation.claim_gate(compacted, 42, gate_ctx) is None


async def test_consent_request_is_stamped_and_claims_no_queue_row(
    monkeypatch: pytest.MonkeyPatch, gate_ctx: MeivaContext
) -> None:
    monkeypatch.setattr(
        impersonation, "native_status", AsyncMock(return_value=_session("requested"))
    )
    before = datetime.now(UTC)
    result = await impersonation.claim_gate(BaseAgentState(), 42, gate_ctx)
    assert result is not None
    message = cast(HumanMessage, cast(dict[str, Any], result.update)["messages"][0])
    kwargs = read_meiva_kwargs(message)
    assert kwargs.get("meiva_msg_type") == "inbound"
    assert message.text[kwargs.get("meiva_inbound_body_start", -1) :].startswith(
        "External agent requests"
    )
    assert "meiva_inbound_id" not in kwargs
    created = kwargs.get("meiva_created_at")
    picked_up = kwargs.get("meiva_picked_up_at")
    assert created is not None and picked_up is not None
    assert before <= datetime.fromisoformat(created) <= datetime.now(UTC)
    assert before <= datetime.fromisoformat(picked_up) <= datetime.now(UTC)
