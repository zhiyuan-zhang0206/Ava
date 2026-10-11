"""The reply-suggestion pipeline against fake stores and models."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from base.agents import AgentNotFound
from base.agents.contract import AgentStatus
from base.lm.catalog import ModelCatalog
from base.lm.catalog.decide.types import (
    DecisionKeyMissingError,
    DecisionRequestError,
    YesNo,
    estimate_tokens,
)
from gateway.agents.reply_suggestion import service as service_module
from gateway.agents.reply_suggestion.service import (
    CACHE_ENTRIES,
    DRAFT_INSTRUCTION,
    DRAFT_USAGE_KIND,
    GATE_USAGE_KIND,
    MAX_SUGGESTION_CHARS,
)
from gateway.agents.reply_suggestion.tests.support import (
    Harness,
    agent_text,
    awaiting,
    conversation,
    inbound,
)


@pytest.fixture
def harness(model_catalog: ModelCatalog) -> Harness:
    return Harness(model_catalog)


@pytest.fixture
def usage_rows(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    def record(message: AIMessage, model: str, **kwargs: Any) -> None:
        rows.append({"model": model, **kwargs})

    monkeypatch.setattr(service_module, "log_usage_from_message", record)
    return rows


async def test_a_running_agent_gets_no_suggestion_and_no_model_is_called(harness: Harness) -> None:
    harness.status = AgentStatus.RUNNING

    result = await harness.service.suggest(7)

    assert (result.suggestion, result.reason) == (None, "agent_not_idle")
    assert harness.loads == []
    assert harness.decide.calls == []
    assert harness.chat.invocations == []


async def test_an_unknown_agent_fails_the_request(harness: Harness) -> None:
    def missing(agent_id: int) -> AgentStatus:
        raise AgentNotFound(f"agent {agent_id} does not exist")

    harness.service._agent_status = missing

    with pytest.raises(AgentNotFound):
        await harness.service.suggest(404)


async def test_the_cluster_switch_turns_the_pipeline_off(harness: Harness) -> None:
    harness.settings.enabled = False

    result = await harness.service.suggest(7)

    assert result.reason == "disabled"
    assert harness.decide.calls == []


async def test_a_gate_below_the_threshold_returns_no_suggestion(harness: Harness) -> None:
    harness.decide.result = awaiting(0.2)

    result = await harness.service.suggest(7)

    assert (result.suggestion, result.reason) == (None, "not_awaiting_reply")
    assert len(harness.decide.calls) == 1
    assert harness.chat.invocations == []


async def test_a_gate_exactly_at_the_threshold_passes(harness: Harness) -> None:
    harness.decide.result = awaiting(0.5)

    result = await harness.service.suggest(7)

    assert result.suggestion == "Yes, go ahead."


async def test_the_gate_asks_its_question_about_the_excerpt_and_is_billed_to_the_agent(
    harness: Harness,
) -> None:
    await harness.service.suggest(7)

    model, state, questions, kwargs = harness.decide.calls[0]
    assert model == "gate-model"
    assert state == "[User]\nPlease prepare the release.\n\n[Assistant]\nDeploy now?"
    assert list(questions) == ["awaiting"]
    assert isinstance(questions["awaiting"], YesNo)
    assert kwargs["usage_kind"] == GATE_USAGE_KIND == "reply_suggestion_gate"
    assert kwargs["for_agent_id"] == 7
    assert kwargs["catalog"] is harness.service._catalog


async def test_a_passing_gate_drafts_with_the_instruction_first(
    harness: Harness, usage_rows: list[dict[str, Any]]
) -> None:
    harness.chat.reply = "  Yes, deploy it.\n"

    result = await harness.service.suggest(7)

    assert (result.suggestion, result.reason) == ("Yes, deploy it.", None)
    assert harness.chat.built == ["chat-model"]
    (messages,) = harness.chat.invocations
    assert [type(m) for m in messages] == [SystemMessage, HumanMessage]
    assert messages[0].content == DRAFT_INSTRUCTION
    assert messages[1].content == harness.decide.calls[0][1]
    assert usage_rows == [
        {
            "model": "chat-model",
            "usage_kind": DRAFT_USAGE_KIND,
            "latency_ms": usage_rows[0]["latency_ms"],
            "for_agent_id": 7,
            "catalog": harness.service._catalog,
        }
    ]
    assert DRAFT_USAGE_KIND == "reply_suggestion"


async def test_an_oversized_conversation_is_cut_to_the_input_budget(harness: Harness) -> None:
    harness.settings.max_input_tokens = 120
    harness.messages = [
        SystemMessage(content="system prompt"),
        inbound("question " * 300),
        agent_text("filler " * 600 + "Shall I deploy?"),
    ]

    await harness.service.suggest(7)

    state = harness.decide.calls[0][1]
    assert estimate_tokens(state) <= 120
    assert state.endswith("Shall I deploy?")
    assert harness.chat.invocations[0][1].content == state


async def test_a_completed_result_is_served_from_the_cache(harness: Harness) -> None:
    first = await harness.service.suggest(7)
    second = await harness.service.suggest(7)

    assert second == first
    assert len(harness.decide.calls) == 1
    assert len(harness.chat.invocations) == 1


async def test_a_gate_refusal_is_cached_too(harness: Harness) -> None:
    harness.decide.result = awaiting(0.1)

    await harness.service.suggest(7)
    await harness.service.suggest(7)

    assert len(harness.decide.calls) == 1


async def test_a_new_agent_message_is_a_new_cache_entry(harness: Harness) -> None:
    await harness.service.suggest(7)
    harness.messages = [*conversation(), inbound("yes"), agent_text("Done. Anything else?", "ai-2")]

    await harness.service.suggest(7)

    assert len(harness.decide.calls) == 2


async def test_two_agents_do_not_share_cache_entries(harness: Harness) -> None:
    await harness.service.suggest(7)
    await harness.service.suggest(8)

    assert len(harness.decide.calls) == 2


async def test_the_cache_is_bounded(harness: Harness) -> None:
    for agent_id in range(CACHE_ENTRIES + 5):
        await harness.service.suggest(agent_id)

    assert len(harness.service._cache) == CACHE_ENTRIES


async def test_a_gate_failure_degrades_to_no_suggestion_with_a_warning(
    harness: Harness, loguru_records: list[dict[str, Any]]
) -> None:
    harness.decide.error = DecisionRequestError("provider down")

    result = await harness.service.suggest(7)

    assert (result.suggestion, result.reason) == (None, "gate_failed")
    assert harness.chat.invocations == []
    warnings = [r for r in loguru_records if r["level"].name == "WARNING"]
    assert [r["extra"]["event"] for r in warnings] == ["reply_suggestion_gate_failed"]


async def test_a_failure_is_not_cached_so_the_next_request_retries(harness: Harness) -> None:
    harness.decide.error = DecisionRequestError("provider down")
    await harness.service.suggest(7)
    harness.decide.error = None

    result = await harness.service.suggest(7)

    assert result.suggestion == "Yes, go ahead."
    assert len(harness.decide.calls) == 2


async def test_a_draft_failure_degrades_to_no_suggestion_with_a_warning(
    harness: Harness, loguru_records: list[dict[str, Any]]
) -> None:
    harness.chat.error = httpx.ReadTimeout("slow")

    result = await harness.service.suggest(7)

    assert (result.suggestion, result.reason) == (None, "generation_failed")
    warnings = [r for r in loguru_records if r["level"].name == "WARNING"]
    assert [r["extra"]["event"] for r in warnings] == ["reply_suggestion_draft_failed"]


@pytest.mark.parametrize("reply", ["", "   \n", "x" * (MAX_SUGGESTION_CHARS + 1)])
async def test_an_empty_or_overlong_draft_is_rejected_not_trimmed(
    harness: Harness, reply: str
) -> None:
    harness.chat.reply = reply

    result = await harness.service.suggest(7)

    assert (result.suggestion, result.reason) == (None, "invalid_generation")


async def test_a_conversation_without_agent_text_has_nothing_to_suggest(harness: Harness) -> None:
    harness.messages = [SystemMessage(content="system prompt"), inbound("hello")]

    result = await harness.service.suggest(7)

    assert result.reason == "no_agent_text"
    assert harness.decide.calls == []


async def test_an_unclassified_draft_error_is_not_swallowed(harness: Harness) -> None:
    harness.chat.error = ValueError("a bug, not a provider failure")

    with pytest.raises(ValueError, match="a bug"):
        await harness.service.suggest(7)


async def test_a_missing_gate_key_degrades_like_any_decision_error(
    harness: Harness, loguru_records: list[dict[str, Any]]
) -> None:
    harness.decide.error = DecisionKeyMissingError("TYPESAFE_API_KEY is not set")

    result = await harness.service.suggest(7)

    assert (result.suggestion, result.reason) == (None, "gate_failed")
    assert harness.chat.invocations == []


async def test_an_error_outside_the_decision_family_is_not_swallowed(harness: Harness) -> None:
    harness.decide.error = RuntimeError("not a DecisionError")

    with pytest.raises(RuntimeError, match="not a DecisionError"):
        await harness.service.suggest(7)
