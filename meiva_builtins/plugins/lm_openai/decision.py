"""OpenAI Decisions API binding (``POST /v1/decisions``, model ``gpt-6-luna``).

Wire shape per the Decisions guide (https://developers.openai.com/api/docs/guides/decisions):
``input`` is the shared text, ``questions`` an array of ``predicate`` / ``choice`` / ``score``
questions each carrying a unique ``name``, and ``answers`` come back in question order echoing
that name. One request carries every question, so nothing is split or fanned out.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from base.lm.provider_api import (
    FALSE,
    TRUE,
    Choice,
    Decision,
    DecisionBuildContext,
    DecisionResponseError,
    DecisionResult,
    Question,
    Score,
    YesNo,
    post_json,
)

ENDPOINT = "https://api.openai.com/v1/decisions"
WIRE_MODEL = "gpt-6-luna"


class DecisionsBinding:
    """The Decisions API client for one call."""

    def __init__(self, ctx: DecisionBuildContext, api_key: str) -> None:
        self._ctx = ctx
        self._api_key = api_key

    async def decide(self, state: str, questions: Mapping[str, Question]) -> DecisionResult:
        payload = {
            "model": WIRE_MODEL,
            "input": state,
            "questions": [_wire_question(qid, q) for qid, q in questions.items()],
        }
        body = await post_json(self._ctx, url=ENDPOINT, api_key=self._api_key, payload=payload)
        tokens = _input_tokens(body)
        try:
            return DecisionResult(_parse_answers(body, questions), tokens)
        except DecisionResponseError as exc:
            raise DecisionResponseError(str(exc), input_tokens=tokens) from exc


def _wire_question(name: str, question: Question) -> dict[str, Any]:
    match question:
        case YesNo():
            return {
                "type": "predicate",
                "name": name,
                "instructions": (
                    f"{question.question}\nAnswer true if: {question.true_if}\n"
                    f"Answer false if: {question.false_if}"
                ),
            }
        case Choice():
            return {
                "type": "choice",
                "name": name,
                "instructions": question.question,
                "choices": [
                    {"value": option, "description": description}
                    for option, description in question.options.items()
                ],
            }
        case Score():
            return {
                "type": "score",
                "name": name,
                "instructions": question.question,
                "levels": [
                    {"label": label, "description": description}
                    for label, description in zip(question.scale, question.rubric, strict=True)
                ],
            }


def _object(value: object, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DecisionResponseError(f"Decisions response: {what} is not an object")
    return cast(dict[str, Any], value)


def _array(value: object, what: str) -> list[object]:
    if not isinstance(value, list):
        raise DecisionResponseError(f"Decisions response: {what} is not an array")
    return cast(list[object], value)


def _input_tokens(body: Mapping[str, Any]) -> int:
    usage = body.get("usage")
    tokens = cast(dict[str, Any], usage).get("input_tokens") if isinstance(usage, dict) else None
    if not isinstance(tokens, int) or isinstance(tokens, bool) or tokens < 0:
        raise DecisionResponseError("Decisions response carries no valid usage.input_tokens")
    return tokens


def _number(value: object, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise DecisionResponseError(f"Decisions response: {what} is not a number: {value!r}")
    return float(value)


def _parse_answers(
    body: Mapping[str, Any], questions: Mapping[str, Question]
) -> dict[str, Decision]:
    decisions: dict[str, Decision] = {}
    for item in _array(body.get("answers"), "answers"):
        answer = _object(item, "answer")
        name = answer.get("name")
        if not isinstance(name, str) or name not in questions:
            raise DecisionResponseError(f"Decisions answered unknown question {name!r}")
        if name in decisions:
            raise DecisionResponseError(f"Decisions answered question {name!r} twice")
        if answer.get("type") == "refusal":
            raise DecisionResponseError(f"Decisions model refused question {name!r}")
        decisions[name] = _parse_answer(name, questions[name], answer)
    return decisions


def _parse_answer(name: str, question: Question, answer: Mapping[str, Any]) -> Decision:
    expected = {YesNo: "predicate", Choice: "choice", Score: "score"}[type(question)]
    if answer.get("type") != expected:
        raise DecisionResponseError(
            f"Decisions answer {name!r} has type {answer.get('type')!r}, expected {expected!r}"
        )
    if isinstance(question, YesNo):
        p_true = _number(answer.get("probability"), f"{name}.probability")
        return Decision(
            top=TRUE if p_true >= 0.5 else FALSE,
            probabilities={TRUE: p_true, FALSE: 1.0 - p_true},
        )
    raw = _array(answer.get("probabilities"), f"{name}.probabilities")
    confidence = _number(answer.get("confidence"), f"{name}.confidence")
    if isinstance(question, Choice):
        top = answer.get("choice")
        if not isinstance(top, str):
            raise DecisionResponseError(f"Decisions answer {name!r} carries no choice")
        probabilities = {
            _entry_key(name, entry, "value"): _number(
                _object(entry, f"{name}.probabilities entry").get("probability"), f"{name}.p"
            )
            for entry in raw
        }
        return Decision(top=top, probabilities=probabilities, confidence=confidence)
    by_label: dict[str, float] = {}
    for entry in raw:
        label = _entry_key(name, entry, "label")
        by_label[label] = _number(
            _object(entry, f"{name}.probabilities entry").get("probability"),
            f"{name}.probability[{label}]",
        )
    return Decision(
        top=max(by_label, key=lambda label: by_label[label]),
        probabilities=by_label,
        confidence=confidence,
        score=_number(answer.get("score"), f"{name}.score"),
    )


def _entry_key(name: str, entry: object, field: str) -> str:
    key = _object(entry, f"{name}.probabilities entry").get(field)
    if not isinstance(key, str):
        raise DecisionResponseError(f"Decisions answer {name!r}: probability entry lacks {field!r}")
    return key
