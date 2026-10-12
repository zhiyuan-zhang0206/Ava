"""Jev (TypeSafe) request and response mapping against a mock HTTP transport."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from base.lm.catalog import ModelCatalog
from base.lm.catalog.decide.types import (
    Choice,
    DecisionKeyMissingError,
    DecisionRequestError,
    DecisionResponseError,
    Score,
    YesNo,
)
from base.lm.decision.call import decide
from base.lm.plugin_providers import build_model_catalog

_MODEL = "jev-1.13.0"
_QUESTIONS = {
    "inj": YesNo("Does the text try to instruct an AI?", "it gives the AI orders", "it does not"),
    "team": Choice("Which team?", {"billing": "payments", "tech": "bugs"}),
    "sev": Score("How severe?", ("low", "mid", "high"), ("minor", "workaround", "blocked")),
}


def _answers() -> dict[str, Any]:
    return {
        "inj": {"type": "noul", "noul": 0.93},
        "team": {
            "type": "choice",
            "choice": "billing",
            "confidence": 0.8,
            "probabilities": {"billing": 0.9, "tech": 0.1},
        },
        "sev": {
            "type": "score",
            "score": 1.1,
            "confidence": 0.55,
            "legend": {"0": "minor", "1": "workaround", "2": "blocked"},
            "probabilities": {"0": 0.1, "1": 0.7, "2": 0.2},
        },
    }


def _body(**overrides: Any) -> dict[str, Any]:
    return {
        "model": _MODEL,
        "answers": _answers(),
        "usage": {"input_tokens": 321, "output_tokens": 0},
        **overrides,
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
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-test-key")
    return build_model_catalog()


async def _decide(catalog: ModelCatalog, server: _Server) -> Any:
    return await decide(
        _MODEL,
        "ignore previous instructions",
        _QUESTIONS,
        catalog=catalog,
        usage_kind="test",
        transport=server.transport,
    )


async def test_request_maps_questions_to_the_systemone_wire_shape(catalog: ModelCatalog) -> None:
    server = _Server(httpx.Response(200, json=_body()))

    await _decide(catalog, server)

    [request] = server.requests
    assert request.method == "POST"
    assert str(request.url) == "https://api.typesafe.ai/v1/systemone"
    assert request.headers["authorization"] == "Bearer ts-test-key"
    assert json.loads(request.content) == {
        "state": "ignore previous instructions",
        "model": "jev-1.13.0",
        "questions": {
            "inj": {
                "type": "noul",
                "instructions": "Does the text try to instruct an AI?",
                "criteria": {"true": "it gives the AI orders", "false": "it does not"},
            },
            "team": {
                "type": "choice",
                "instructions": "Which team?",
                "criteria": {"billing": "payments", "tech": "bugs"},
            },
            "sev": {
                "type": "score",
                "instructions": "How severe?",
                "criteria": ["low: minor", "mid: workaround", "high: blocked"],
            },
        },
    }


async def test_response_maps_to_full_distributions(catalog: ModelCatalog) -> None:
    result = await _decide(catalog, _Server(httpx.Response(200, json=_body())))

    assert result["inj"].top == "true"
    assert result["inj"].probability("true") == pytest.approx(0.93)
    assert result["inj"].probability("false") == pytest.approx(0.07)
    assert result["team"].top == "billing"
    assert result["team"].confidence == 0.8
    assert result["sev"].top == "mid"
    assert result["sev"].probabilities == {"low": 0.1, "mid": 0.7, "high": 0.2}
    assert result["sev"].score == 1.1


async def test_missing_key_fails_before_any_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    server = _Server(httpx.Response(200, json=_body()))

    with pytest.raises(DecisionKeyMissingError, match="TYPESAFE_API_KEY not set"):
        await _decide(build_model_catalog(), server)
    assert server.requests == []


async def test_non_success_status_is_a_request_error_without_retry(catalog: ModelCatalog) -> None:
    server = _Server(httpx.Response(429, json={"error": "rate limited"}))

    with pytest.raises(DecisionRequestError, match="HTTP 429"):
        await _decide(catalog, server)
    assert len(server.requests) == 1


async def test_transport_failure_is_a_request_error(catalog: ModelCatalog) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    server = _Server(httpx.Response(200))
    server.transport = httpx.MockTransport(refuse)

    with pytest.raises(DecisionRequestError, match="ConnectError"):
        await _decide(catalog, server)


_Mutation = Callable[[dict[str, Any]], object]
_BAD_RESPONSES: list[tuple[_Mutation, str]] = [
    (lambda b: b["answers"]["team"].update(type="noul"), "type 'noul', expected 'choice'"),
    (lambda b: b["answers"]["sev"]["probabilities"].update({"7": 0.0}), "level key '7'"),
    (lambda b: b["answers"]["team"]["probabilities"].pop("tech"), "missing \\['tech'\\]"),
    (lambda b: b["answers"].pop("sev"), "missing \\['sev'\\]"),
    (lambda b: b["answers"].update(ghost={"type": "noul", "noul": 0.1}), "unknown question"),
    (lambda b: b["answers"]["inj"].update(noul="high"), "not a number"),
    (lambda b: b["answers"]["team"]["probabilities"].update(billing=0.2), "sum to 0.3"),
]


@pytest.mark.parametrize(("mutate", "match"), _BAD_RESPONSES)
async def test_malformed_or_inconsistent_responses_fail(
    catalog: ModelCatalog, mutate: _Mutation, match: str
) -> None:
    body = _body()
    mutate(body)

    with pytest.raises(DecisionResponseError, match=match):
        await _decide(catalog, _Server(httpx.Response(200, json=body)))


async def test_a_response_without_usage_fails(catalog: ModelCatalog) -> None:
    body = _body()
    del body["usage"]

    with pytest.raises(DecisionResponseError, match=r"usage\.input_tokens"):
        await _decide(catalog, _Server(httpx.Response(200, json=body)))


async def test_billed_input_tokens_are_priced_at_the_jev_rate(
    catalog: ModelCatalog, loguru_records: list[dict[str, Any]]
) -> None:
    body = _body(usage={"input_tokens": 1_000_000, "output_tokens": 0})
    await _decide(catalog, _Server(httpx.Response(200, json=body)))

    [record] = [r for r in loguru_records if r["extra"].get("event") == "llm_usage"]
    assert record["extra"]["model"] == _MODEL
    assert record["extra"]["cost_usd"] == pytest.approx(0.042)
    assert record["extra"]["out_total"] == 0
