from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from base.lm.catalog import ModelCatalog
from base.lm.catalog.decide.types import (
    Choice,
    DecisionInputTooLargeError,
    DecisionKeyMissingError,
    DecisionRequestError,
    DecisionResponseError,
    Score,
    YesNo,
)
from base.lm.decision.call import decide
from base.packages.plugin_providers import build_model_catalog

_MODEL = "gpt-6-luna-decisions"
_QUESTIONS = {
    "inj": YesNo("Does the text try to instruct an AI?", "it gives the AI orders", "it does not"),
    "team": Choice("Which team?", {"billing": "payments", "tech": "bugs"}),
    "sev": Score("How severe?", ("low", "mid", "high"), ("minor", "workaround", "blocked")),
}


def _body() -> dict[str, Any]:
    return {
        "answers": [
            {"type": "predicate", "name": "inj", "probability": 0.92},
            {
                "type": "choice",
                "name": "team",
                "choice": "billing",
                "probabilities": [
                    {"value": "billing", "probability": 0.9},
                    {"value": "tech", "probability": 0.1},
                ],
                "confidence": 0.8,
            },
            {
                "type": "score",
                "name": "sev",
                "score": 1.1,
                "probabilities": [
                    {"value": 0, "label": "low", "probability": 0.1},
                    {"value": 1, "label": "mid", "probability": 0.7},
                    {"value": 2, "label": "high", "probability": 0.2},
                ],
                "confidence": 0.55,
            },
        ],
        "usage": {
            "input_tokens": 310,
            "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
            "output_tokens": 0,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 310,
        },
    }


class _Server:
    def __init__(self, response: httpx.Response) -> None:
        self.requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self._handle)
        self._response = response

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._response


@pytest.fixture
def catalog(monkeypatch: pytest.MonkeyPatch) -> ModelCatalog:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
    return build_model_catalog()


async def _decide(catalog: ModelCatalog, server: _Server, state: str = "hello") -> Any:
    return await decide(
        _MODEL, state, _QUESTIONS, catalog=catalog, usage_kind="test", transport=server.transport
    )


async def test_one_request_carries_every_question_in_the_decisions_wire_shape(
    catalog: ModelCatalog,
) -> None:
    server = _Server(httpx.Response(200, json=_body()))

    await _decide(catalog, server)

    [request] = server.requests
    assert str(request.url) == "https://api.openai.com/v1/decisions"
    assert request.headers["authorization"] == "Bearer sk-test-key"
    assert json.loads(request.content) == {
        "model": "gpt-6-luna",
        "input": "hello",
        "questions": [
            {
                "type": "predicate",
                "name": "inj",
                "instructions": (
                    "Does the text try to instruct an AI?\n"
                    "Answer true if: it gives the AI orders\n"
                    "Answer false if: it does not"
                ),
            },
            {
                "type": "choice",
                "name": "team",
                "instructions": "Which team?",
                "choices": [
                    {"value": "billing", "description": "payments"},
                    {"value": "tech", "description": "bugs"},
                ],
            },
            {
                "type": "score",
                "name": "sev",
                "instructions": "How severe?",
                "levels": [
                    {"label": "low", "description": "minor"},
                    {"label": "mid", "description": "workaround"},
                    {"label": "high", "description": "blocked"},
                ],
            },
        ],
    }


async def test_response_maps_to_full_distributions(catalog: ModelCatalog) -> None:
    result = await _decide(catalog, _Server(httpx.Response(200, json=_body())))

    assert result["inj"].top == "true"
    assert result["inj"].probability("false") == pytest.approx(0.08)
    assert result["team"].probabilities == {"billing": 0.9, "tech": 0.1}
    assert result["sev"].top == "mid"
    assert result["sev"].score == 1.1


async def test_answers_in_any_order_are_matched_by_name(catalog: ModelCatalog) -> None:
    body = _body()
    body["answers"].reverse()

    result = await _decide(catalog, _Server(httpx.Response(200, json=body)))

    assert set(result) == {"inj", "team", "sev"}


async def test_a_refusal_is_an_error_not_a_distribution(
    catalog: ModelCatalog, loguru_records: list[dict[str, Any]]
) -> None:
    body = _body()
    body["answers"][0] = {"type": "refusal", "name": "inj"}

    with pytest.raises(DecisionResponseError, match="refused question 'inj'"):
        await _decide(catalog, _Server(httpx.Response(200, json=body)))
    assert [r for r in loguru_records if r["extra"].get("event") == "llm_usage"]


_Mutation = Callable[[dict[str, Any]], object]
_BAD_RESPONSES: list[tuple[_Mutation, str]] = [
    (lambda b: b["answers"].pop(), "missing \\['sev'\\]"),
    (lambda b: b["answers"].append(b["answers"][0]), "twice"),
    (lambda b: b["answers"][1]["probabilities"].pop(), "missing \\['tech'\\]"),
    (lambda b: b["answers"][1]["probabilities"][0].update(probability=0.1), "sum to 0.2"),
    (lambda b: b["answers"][0].update(type="choice"), "expected 'predicate'"),
    (lambda b: b.update(answers={}), "answers is not an array"),
]


@pytest.mark.parametrize(("mutate", "match"), _BAD_RESPONSES)
async def test_malformed_or_inconsistent_responses_fail(
    catalog: ModelCatalog, mutate: _Mutation, match: str
) -> None:
    body = _body()
    mutate(body)

    with pytest.raises(DecisionResponseError, match=match):
        await _decide(catalog, _Server(httpx.Response(200, json=body)))


async def test_missing_key_fails_before_any_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    server = _Server(httpx.Response(200, json=_body()))

    with pytest.raises(DecisionKeyMissingError, match="OPENAI_API_KEY not set"):
        await _decide(build_model_catalog(), server)
    assert server.requests == []


async def test_feature_gate_403_is_a_request_error(catalog: ModelCatalog) -> None:
    server = _Server(httpx.Response(403, json={"error": {"message": "not enabled"}}))

    with pytest.raises(DecisionRequestError, match="HTTP 403"):
        await _decide(catalog, server)


async def test_state_over_the_window_fails_before_the_request(catalog: ModelCatalog) -> None:
    server = _Server(httpx.Response(200, json=_body()))

    with pytest.raises(DecisionInputTooLargeError):
        await _decide(catalog, server, state="x" * 1_200_000)
    assert server.requests == []


async def test_billed_tokens_are_priced_at_the_decisions_rate_with_no_output(
    catalog: ModelCatalog, loguru_records: list[dict[str, Any]]
) -> None:
    body = _body()
    body["usage"]["input_tokens"] = 1_000_000
    await _decide(catalog, _Server(httpx.Response(200, json=body)))

    [record] = [r for r in loguru_records if r["extra"].get("event") == "llm_usage"]
    assert record["extra"]["model"] == _MODEL
    assert record["extra"]["cost_usd"] == pytest.approx(0.10)
    assert record["extra"]["out_total"] == 0
