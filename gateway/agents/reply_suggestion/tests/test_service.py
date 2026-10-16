from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.output_parsers import JsonOutputParser

from base.api_contracts.agent_contract import AgentNotFound, AgentStatus
from base.lm.catalog import ModelCatalog
from base.lm.catalog.decide.types import (
    DecisionKeyMissingError,
    DecisionRequestError,
    YesNo,
    estimate_tokens,
)
from gateway.agents.reply_suggestion import service as service_module
from gateway.agents.reply_suggestion.schemas import MAX_SUGGESTION_CHARS, DraftReplies
from gateway.agents.reply_suggestion.service import (
    CACHE_ENTRIES,
    DRAFT_INSTRUCTION,
    DRAFT_USAGE_KIND,
    GATE_USAGE_KIND,
)
from gateway.agents.reply_suggestion.tests.support import (
    Harness,
    agent_text,
    conversation,
    inbound,
    useful_reply,
)


@pytest.fixture
def harness(model_catalog: ModelCatalog) -> Harness:
    return Harness(model_catalog)


@pytest.fixture
def usage_rows(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    def record(message: AIMessage, model: str, **kwargs: Any) -> None:
        rows.append({"message": message, "model": model, **kwargs})

    monkeypatch.setattr(service_module, "log_usage_from_message", record)
    return rows


async def test_a_running_agent_gets_no_suggestion_and_no_model_is_called(harness: Harness) -> None:
    harness.status = AgentStatus.RUNNING

    result = await harness.service.suggest(7)

    assert (result.suggestions, result.reason) == ([], "agent_not_idle")
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
    harness.decide.result = useful_reply(0.2)

    result = await harness.service.suggest(7)

    assert (result.suggestions, result.reason) == ([], "not_useful")
    assert len(harness.decide.calls) == 1
    assert harness.chat.invocations == []


async def test_a_gate_exactly_at_the_threshold_passes(harness: Harness) -> None:
    harness.decide.result = useful_reply(0.5)

    result = await harness.service.suggest(7)

    assert result.suggestions == ["Yes, go ahead.", "Wait, explain the plan first."]


async def test_the_gate_asks_its_question_about_the_excerpt_and_is_billed_to_the_agent(
    harness: Harness,
) -> None:
    await harness.service.suggest(7)

    model, state, questions, kwargs = harness.decide.calls[0]
    assert model == "gate-model"
    assert state == "[User]\nPlease prepare the release.\n\n[Assistant]\nDeploy now?"
    assert list(questions) == ["useful_reply"]
    assert isinstance(questions["useful_reply"], YesNo)
    assert kwargs["usage_kind"] == GATE_USAGE_KIND == "reply_suggestion_gate"
    assert kwargs["for_agent_id"] == 7
    assert kwargs["catalog"] is harness.service._catalog


async def test_a_passing_gate_drafts_with_the_instruction_first(
    harness: Harness, usage_rows: list[dict[str, Any]]
) -> None:
    harness.chat.reply = '{"suggestions": ["  Yes, deploy it.  ", "Wait, explain the plan first."]}'

    result = await harness.service.suggest(7)

    assert (result.suggestions, result.reason) == (
        ["Yes, deploy it.", "Wait, explain the plan first."],
        None,
    )
    assert harness.chat.built == ["chat-model"]
    assert harness.chat.bindings == [
        (
            DraftReplies.model_json_schema(),
            {"method": "json_schema", "strict": True, "include_raw": True},
        )
    ]
    (messages,) = harness.chat.invocations
    assert [type(m) for m in messages] == [SystemMessage, HumanMessage]
    assert messages[0].content == DRAFT_INSTRUCTION
    assert messages[1].content == harness.decide.calls[0][1]
    assert usage_rows == [
        {
            "message": harness.chat.raw,
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
        inbound("question " * 300 + "the-last-message"),
        agent_text("filler " * 600 + "Shall I deploy?"),
    ]

    await harness.service.suggest(7)

    state = harness.decide.calls[0][1]
    assert estimate_tokens(state) <= 120
    assert state.endswith("the-last-message")
    assert harness.chat.invocations[0][1].content == state


async def test_a_completed_result_is_served_from_the_cache(harness: Harness) -> None:
    first = await harness.service.suggest(7)
    second = await harness.service.suggest(7)

    assert second == first
    assert len(harness.decide.calls) == 1
    assert len(harness.chat.invocations) == 1


async def test_a_gate_refusal_is_cached_too(harness: Harness) -> None:
    harness.decide.result = useful_reply(0.1)

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

    assert (result.suggestions, result.reason) == ([], "gate_failed")
    assert harness.chat.invocations == []
    warnings = [r for r in loguru_records if r["level"].name == "WARNING"]
    assert [r["extra"]["event"] for r in warnings] == ["reply_suggestion_gate_failed"]


async def test_a_failure_is_not_cached_so_the_next_request_retries(harness: Harness) -> None:
    harness.decide.error = DecisionRequestError("provider down")
    await harness.service.suggest(7)
    harness.decide.error = None

    result = await harness.service.suggest(7)

    assert result.suggestions == ["Yes, go ahead.", "Wait, explain the plan first."]
    assert len(harness.decide.calls) == 2


async def test_a_draft_failure_degrades_to_no_suggestion_with_a_warning(
    harness: Harness, loguru_records: list[dict[str, Any]]
) -> None:
    harness.chat.error = httpx.ReadTimeout("slow")

    result = await harness.service.suggest(7)

    assert (result.suggestions, result.reason) == ([], "generation_failed")
    warnings = [r for r in loguru_records if r["level"].name == "WARNING"]
    assert [r["extra"]["event"] for r in warnings] == ["reply_suggestion_draft_failed"]


@pytest.mark.parametrize(
    "reply",
    [
        "",
        "not JSON",
        json.dumps({"suggestions": []}),
        json.dumps({"suggestions": ["   \n"]}),
        json.dumps({"suggestions": ["x" * (MAX_SUGGESTION_CHARS + 1)]}),
        json.dumps({"suggestions": ["Yes", " yes "]}),
        json.dumps({"suggestions": ["Yes", 42]}),
        json.dumps(["Yes"]),
        json.dumps({}),
        json.dumps({"suggestions": ["Yes"], "unexpected": "extra"}),
    ],
)
async def test_invalid_candidates_are_rejected_as_a_whole(harness: Harness, reply: str) -> None:
    harness.chat.reply = reply

    result = await harness.service.suggest(7)

    assert (result.suggestions, result.reason) == ([], "invalid_generation")


@pytest.mark.parametrize(
    "candidates",
    [
        ["Continue."],
        ["Continue.", "Pause."],
        ["Use A.", "Use B.", "Use C.", "Use D."],
    ],
)
async def test_model_candidate_count_and_order_reach_the_response(
    harness: Harness, candidates: list[str]
) -> None:
    harness.chat.reply = json.dumps({"suggestions": candidates})

    result = await harness.service.suggest(7)

    assert result.suggestions == candidates
    assert result.reason is None
    assert len(harness.chat.invocations) == 1


@pytest.mark.parametrize(
    "metadata",
    [
        {"model_provider": "openai", "status": "incomplete"},
        {"model_provider": "anthropic", "stop_reason": "max_tokens"},
        {"model_provider": "openai", "status": "failed"},
        {"model_provider": "anthropic"},
    ],
)
async def test_unfinished_structured_output_is_rejected_even_if_the_text_is_valid(
    harness: Harness, metadata: dict[str, str], usage_rows: list[dict[str, Any]]
) -> None:
    harness.chat.metadata = metadata

    result = await harness.service.suggest(7)

    assert (result.suggestions, result.reason) == ([], "invalid_generation")
    assert usage_rows[0]["message"] is harness.chat.raw


async def test_an_incomplete_document_repaired_by_langchain_is_rejected(harness: Harness) -> None:
    raw = AIMessage(
        content='{"suggestions": ["Use A."]',
        response_metadata={"model_provider": "openai", "status": "completed"},
    )
    parsed = JsonOutputParser().invoke(raw)
    assert parsed == {"suggestions": ["Use A."]}

    async def repaired_reply(messages: Any) -> dict[str, Any]:
        return {"raw": raw, "parsed": parsed, "parsing_error": None}

    harness.chat.ainvoke = repaired_reply

    result = await harness.service.suggest(7)

    assert (result.suggestions, result.reason) == ([], "invalid_generation")


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

    assert (result.suggestions, result.reason) == ([], "gate_failed")
    assert harness.chat.invocations == []


async def test_an_error_outside_the_decision_family_is_not_swallowed(harness: Harness) -> None:
    harness.decide.error = RuntimeError("not a DecisionError")

    with pytest.raises(RuntimeError, match="not a DecisionError"):
        await harness.service.suggest(7)
