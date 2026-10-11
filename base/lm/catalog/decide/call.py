"""``decide()``: the one entry point for asking a decision model typed questions.

Everything provider-independent happens here, around the provider's ``DecisionBinding``:
input validation, the pre-flight size check, usage accounting and validation of the returned
distributions. Callers own truncation and chunking; this module never shortens ``state``.
"""

from __future__ import annotations

import math
import time
from collections.abc import Mapping

import httpx

from base.lm.catalog import ModelCatalog
from base.lm.catalog.decide.types import (
    SUM_TOLERANCE,
    Choice,
    Decision,
    DecisionInputTooLargeError,
    DecisionKeyMissingError,
    DecisionModelSpec,
    DecisionResponseError,
    Question,
    Score,
    UnknownDecisionModelError,
    YesNo,
    estimate_tokens,
    question_text,
)
from base.lm.catalog.provider_contract import DecisionBuildContext
from base.lm.provider_api import provider_key_present
from base.lm.usage import log_usage_fields

DEFAULT_TIMEOUT_S = 30.0
_EPSILON = 1e-9


async def decide(
    model: str,
    state: str,
    questions: Mapping[str, Question],
    *,
    catalog: ModelCatalog,
    usage_kind: str,
    source: str | None = None,
    for_agent_id: int | None = None,
    timeout: float | None = DEFAULT_TIMEOUT_S,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Mapping[str, Decision]:
    """Ask ``model`` every question about ``state``; return one ``Decision`` per question id.

    ``usage_kind`` and ``source`` label the one ``llm_usage`` row this call writes (output
    tokens are always 0). Raises, without sending anything, for an unknown model id, invalid
    questions or an estimated input above the model's limits; raises ``DecisionKeyMissingError``
    when the provider's API key is not configured; raises ``DecisionRequestError`` /
    ``DecisionResponseError`` when the provider fails or returns a distribution that is
    incomplete, has unknown outcomes or does not sum to 1.
    """
    _check_inputs(state, questions)
    spec = _spec_of(model, catalog)
    _check_size(model, spec, state, questions)
    provider = catalog.decision_provider_of(model)
    if provider is None:
        raise UnknownDecisionModelError(f"no decision provider owns the prefix of {model!r}")
    try:
        binding = provider.build(
            DecisionBuildContext(model=model, spec=spec, timeout=timeout, transport=transport)
        )
    except RuntimeError as exc:
        # `require_key` raises a plain RuntimeError; only a build that failed with its key
        # absent is a key problem, any other build failure propagates unchanged.
        if provider_key_present(provider.key_env):
            raise
        raise DecisionKeyMissingError(str(exc)) from exc

    started = time.monotonic()
    try:
        result = await binding.decide(state, questions)
    except DecisionResponseError as exc:
        if exc.input_tokens is not None:
            _log_usage(model, exc.input_tokens, started, usage_kind, source, for_agent_id, catalog)
        raise
    _log_usage(model, result.input_tokens, started, usage_kind, source, for_agent_id, catalog)
    validate_decisions(questions, result.decisions)
    return result.decisions


def _log_usage(
    model: str,
    input_tokens: int,
    started: float,
    usage_kind: str,
    source: str | None,
    for_agent_id: int | None,
    catalog: ModelCatalog,
) -> None:
    log_usage_fields(
        model,
        tok_in=input_tokens,
        tok_out=0,
        latency_ms=(time.monotonic() - started) * 1000,
        usage_kind=usage_kind,
        source=source,
        for_agent_id=for_agent_id,
        catalog=catalog,
    )


def _check_inputs(state: str, questions: Mapping[str, Question]) -> None:
    if not isinstance(state, str) or not state.strip():
        raise ValueError("decide() state must be a non-empty string")
    if not questions:
        raise ValueError("decide() needs at least one question")
    for question_id, question in questions.items():
        if not isinstance(question_id, str) or not question_id.strip():
            raise ValueError("decide() question ids must be non-empty strings")
        if not isinstance(question, YesNo | Choice | Score):
            raise TypeError(
                f"decide() question {question_id!r} must be YesNo, Choice or Score, "
                f"got {type(question).__name__}"
            )


def _spec_of(model: str, catalog: ModelCatalog) -> DecisionModelSpec:
    spec = catalog.decision_models.get(model)
    if spec is not None:
        return spec
    hint = " (it is a chat model)" if model in catalog.models else ""
    raise UnknownDecisionModelError(
        f"unknown decision model {model!r}{hint}. Available decision models: "
        + ", ".join(sorted(catalog.decision_models))
    )


def _check_size(
    model: str, spec: DecisionModelSpec, state: str, questions: Mapping[str, Question]
) -> None:
    state_tokens = estimate_tokens(state)
    question_tokens = [estimate_tokens(question_text(q)) for q in questions.values()]
    with_longest = state_tokens + max(question_tokens)
    total = state_tokens + sum(question_tokens)
    if with_longest > spec.context_window:
        raise DecisionInputTooLargeError(
            f"{model}: state plus the longest question is ~{with_longest} tokens, over the "
            f"{spec.context_window}-token context window; truncate or chunk the state"
        )
    if total > spec.request_limit:
        raise DecisionInputTooLargeError(
            f"{model}: state plus all questions is ~{total} tokens, over the "
            f"{spec.request_limit}-token request limit; truncate the state or ask fewer questions"
        )


def validate_decisions(
    questions: Mapping[str, Question], decisions: Mapping[str, Decision]
) -> None:
    """Reject a provider response that does not answer exactly the questions asked, with a
    complete, normalized distribution for each."""
    missing = questions.keys() - decisions.keys()
    unknown = decisions.keys() - questions.keys()
    if missing or unknown:
        raise DecisionResponseError(
            f"decision answers do not match the questions: missing {sorted(missing)}, "
            f"unknown {sorted(unknown)}"
        )
    for question_id, question in questions.items():
        _validate_one(question_id, question, decisions[question_id])


def _validate_one(question_id: str, question: Question, decision: Decision) -> None:
    outcomes = set(question.outcomes)
    given = set(decision.probabilities)
    if given != outcomes:
        raise DecisionResponseError(
            f"question {question_id!r}: distribution outcomes differ from the question's: "
            f"missing {sorted(outcomes - given)}, unknown {sorted(given - outcomes)}"
        )
    for outcome, probability in decision.probabilities.items():
        if not math.isfinite(probability) or not -_EPSILON <= probability <= 1 + _EPSILON:
            raise DecisionResponseError(
                f"question {question_id!r}: probability of {outcome!r} is {probability!r}, "
                "outside [0, 1]"
            )
    total = sum(decision.probabilities.values())
    if abs(total - 1.0) > SUM_TOLERANCE:
        raise DecisionResponseError(
            f"question {question_id!r}: probabilities sum to {total:.4f}, not 1"
        )
    if (
        decision.top not in outcomes
        or decision.probabilities[decision.top] < max(decision.probabilities.values()) - _EPSILON
    ):
        raise DecisionResponseError(
            f"question {question_id!r}: top answer {decision.top!r} is not the most probable outcome"
        )
    _validate_extras(question_id, question, decision)


def _validate_extras(question_id: str, question: Question, decision: Decision) -> None:
    if isinstance(question, YesNo):
        if decision.confidence is not None or decision.score is not None:
            raise DecisionResponseError(
                f"question {question_id!r}: a YesNo decision carries no confidence or score"
            )
        return
    if decision.confidence is None or not 0.0 <= decision.confidence <= 1.0:
        raise DecisionResponseError(
            f"question {question_id!r}: confidence {decision.confidence!r} is not in [0, 1]"
        )
    if isinstance(question, Score):
        top_level = len(question.scale) - 1
        if decision.score is None or not -_EPSILON <= decision.score <= top_level + _EPSILON:
            raise DecisionResponseError(
                f"question {question_id!r}: score {decision.score!r} is outside 0..{top_level}"
            )
    elif decision.score is not None:
        raise DecisionResponseError(f"question {question_id!r}: a Choice decision carries no score")
