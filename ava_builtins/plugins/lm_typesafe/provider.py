"""TypeSafe provider plugin for Ava: the Jev decision model.

Jev answers typed questions about a text ``state`` with probabilities and generates no text, so
this plugin contributes a decision provider only. The wire shape follows the TypeSafe HTTP API
reference (https://docs.typesafe.ai/api): ``POST /v1/systemone`` with ``state``, ``model`` and
``questions`` keyed by id; the answers come back under the same ids. The official SDK is not
used; the model id is pinned to a versioned release, never a ``jev-latest`` alias.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from base.lm.provider_api import (
    FALSE,
    TRUE,
    Choice,
    Decision,
    DecisionBinding,
    DecisionBuildContext,
    DecisionModelSpec,
    DecisionProviderContribution,
    DecisionResponseError,
    DecisionResult,
    PriceRates,
    Question,
    Score,
    YesNo,
    post_json,
    require_key,
)
from base.log import logger
from base.packages.plugins.extensions import PluginContributions

_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
_MODEL = "jev-1.13.0"


class JevBinding:
    """One Jev request answers all questions about the state."""

    def __init__(self, ctx: DecisionBuildContext, api_key: str) -> None:
        self._ctx = ctx
        self._api_key = api_key

    async def decide(self, state: str, questions: Mapping[str, Question]) -> DecisionResult:
        payload = {
            "state": state,
            "model": self._ctx.model,
            "questions": {qid: _wire_question(q) for qid, q in questions.items()},
        }
        body = await post_json(self._ctx, url=_ENDPOINT, api_key=self._api_key, payload=payload)
        tokens = _input_tokens(body)
        try:
            return DecisionResult(_parse_answers(body, questions), tokens)
        except DecisionResponseError as exc:
            raise DecisionResponseError(str(exc), input_tokens=tokens) from exc


def _wire_question(question: Question) -> dict[str, Any]:
    match question:
        case YesNo():
            return {
                "type": "noul",
                "instructions": question.question,
                "criteria": {"true": question.true_if, "false": question.false_if},
            }
        case Choice():
            return {
                "type": "choice",
                "instructions": question.question,
                "criteria": dict(question.options),
            }
        case Score():
            return {
                "type": "score",
                "instructions": question.question,
                "criteria": [
                    f"{label}: {description}"
                    for label, description in zip(question.scale, question.rubric, strict=True)
                ],
            }


def _object(value: object, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DecisionResponseError(f"Jev response: {what} is not an object")
    return cast(dict[str, Any], value)


def _input_tokens(body: Mapping[str, Any]) -> int:
    usage = body.get("usage")
    tokens = cast(dict[str, Any], usage).get("input_tokens") if isinstance(usage, dict) else None
    if not isinstance(tokens, int) or isinstance(tokens, bool) or tokens < 0:
        raise DecisionResponseError("Jev response carries no valid usage.input_tokens")
    return tokens


def _number(value: object, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise DecisionResponseError(f"Jev response: {what} is not a number: {value!r}")
    return float(value)


def _parse_answers(
    body: Mapping[str, Any], questions: Mapping[str, Question]
) -> dict[str, Decision]:
    answered = body.get("model")
    if answered != _MODEL:
        logger.warning(
            "Jev answered with model {answered!r}, not the pinned {pinned!r}",
            answered=answered,
            pinned=_MODEL,
        )
    answers = _object(body.get("answers"), "answers")
    decisions: dict[str, Decision] = {}
    for qid, answer in answers.items():
        question = questions.get(qid)
        if question is None:
            raise DecisionResponseError(f"Jev answered unknown question {qid!r}")
        decisions[qid] = _parse_answer(qid, question, _object(answer, f"answer {qid!r}"))
    return decisions


def _parse_answer(qid: str, question: Question, answer: Mapping[str, Any]) -> Decision:
    expected = {YesNo: "noul", Choice: "choice", Score: "score"}[type(question)]
    if answer.get("type") != expected:
        raise DecisionResponseError(
            f"Jev answer {qid!r} has type {answer.get('type')!r}, expected {expected!r}"
        )
    if isinstance(question, YesNo):
        p_true = _number(answer.get("noul"), f"{qid}.noul")
        return Decision(
            top=TRUE if p_true >= 0.5 else FALSE,
            probabilities={TRUE: p_true, FALSE: 1.0 - p_true},
        )
    raw = _object(answer.get("probabilities"), f"{qid}.probabilities")
    confidence = _number(answer.get("confidence"), f"{qid}.confidence")
    if isinstance(question, Choice):
        top = answer.get("choice")
        if not isinstance(top, str):
            raise DecisionResponseError(f"Jev answer {qid!r} carries no choice")
        probabilities = {str(k): _number(v, f"{qid}.probabilities[{k}]") for k, v in raw.items()}
        return Decision(top=top, probabilities=probabilities, confidence=confidence)
    # Score: probabilities are keyed by the 0-based level number as a string.
    by_label: dict[str, float] = {}
    for key, value in raw.items():
        try:
            label = question.scale[int(key)]
        except (ValueError, IndexError) as exc:
            raise DecisionResponseError(
                f"Jev answer {qid!r} has level key {key!r} outside the {len(question.scale)} levels"
            ) from exc
        by_label[label] = _number(value, f"{qid}.probabilities[{key}]")
    return Decision(
        top=max(by_label, key=lambda label: by_label[label]),
        probabilities=by_label,
        confidence=confidence,
        score=_number(answer.get("score"), f"{qid}.score"),
    )


def build(ctx: DecisionBuildContext) -> DecisionBinding:
    return JevBinding(ctx, require_key("TYPESAFE_API_KEY"))


PROVIDER = DecisionProviderContribution(
    prefix="jev-",
    display_name="TypeSafe",
    key_env="TYPESAFE_API_KEY",
    build=build,
    models={
        # 32k bounds the state plus the longest single question, 64k the state plus all
        # questions; text only (https://docs.typesafe.ai/models).
        _MODEL: DecisionModelSpec(context_window=32_000, max_request_tokens=64_000),
    },
    pricing={
        # $0.042 per million input tokens, output free, no cache discount.
        _MODEL: PriceRates(
            cache_miss=0.042,
            cache_hit=0.042,
            output=0.0,
            source_url="https://docs.typesafe.ai/models",
            source_checked_at="2026-10-11",
            vendor="typesafe",
        ),
    },
)


def contribute() -> PluginContributions:
    return PluginContributions(decision_providers=(PROVIDER,))
