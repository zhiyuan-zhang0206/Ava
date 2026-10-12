"""``decide()``: pre-flight checks, distribution validation and usage accounting."""

from __future__ import annotations

from typing import Any

import pytest

from base.lm.catalog import CatalogBuilder
from base.lm.catalog.decide.types import (
    Choice,
    Decision,
    DecisionInputTooLargeError,
    DecisionKeyMissingError,
    DecisionModelSpec,
    DecisionResponseError,
    Score,
    UnknownDecisionModelError,
    YesNo,
)
from base.lm.decision.call import decide
from base.lm.decision.tests.support import MODEL, FakeBinding, catalog_with, provider
from base.lm.provider_api import require_key

_YES_NO = YesNo("Is it spam?", "it is spam", "it is not spam")
_CHOICE = Choice("Which team?", {"billing": "payments", "tech": "bugs"})
_SCORE = Score("How severe?", ("low", "mid", "high"), ("minor", "workaround", "blocked"))


def _good() -> dict[str, Decision]:
    return {
        "spam": Decision(top="true", probabilities={"true": 0.8, "false": 0.2}),
        "team": Decision(
            top="billing", probabilities={"billing": 0.7, "tech": 0.3}, confidence=0.6
        ),
        "sev": Decision(
            top="mid",
            probabilities={"low": 0.1, "mid": 0.7, "high": 0.2},
            confidence=0.5,
            score=1.1,
        ),
    }


_QUESTIONS = {"spam": _YES_NO, "team": _CHOICE, "sev": _SCORE}


async def test_returns_the_providers_decisions_and_passes_state_and_questions() -> None:
    binding = FakeBinding(_good())
    result = await decide(
        MODEL, "hello", _QUESTIONS, catalog=catalog_with(binding), usage_kind="test"
    )

    assert result == _good()
    assert binding.calls == [("hello", _QUESTIONS)]


async def test_unknown_model_id_fails_before_any_call() -> None:
    binding = FakeBinding(_good())
    with pytest.raises(UnknownDecisionModelError, match="nope-1"):
        await decide(
            "nope-1", "hello", _QUESTIONS, catalog=catalog_with(binding), usage_kind="test"
        )
    assert binding.calls == []


async def test_a_context_overflow_fails_before_the_request() -> None:
    binding = FakeBinding(_good())
    catalog = catalog_with(binding, models={MODEL: DecisionModelSpec(context_window=50)})

    with pytest.raises(DecisionInputTooLargeError, match="context window"):
        await decide(MODEL, "x" * 4_000, _QUESTIONS, catalog=catalog, usage_kind="test")
    assert binding.calls == []


async def test_the_total_request_limit_is_checked_separately() -> None:
    binding = FakeBinding(_good())
    spec = DecisionModelSpec(context_window=60, max_request_tokens=70)
    catalog = catalog_with(binding, models={MODEL: spec})

    # State plus the longest question fits 60, state plus all questions does not fit 70.
    with pytest.raises(DecisionInputTooLargeError, match="request limit"):
        await decide(MODEL, "x" * 160, _QUESTIONS, catalog=catalog, usage_kind="test")
    assert binding.calls == []


@pytest.mark.parametrize(
    ("state", "questions", "match"),
    [
        ("", _QUESTIONS, "state"),
        ("hello", {}, "at least one question"),
        ("hello", {"": _YES_NO}, "ids"),
        ("hello", {"q": "not a question"}, "YesNo, Choice or Score"),
    ],
)
async def test_invalid_inputs_are_rejected(state: str, questions: Any, match: str) -> None:
    binding = FakeBinding(_good())
    with pytest.raises((ValueError, TypeError), match=match):
        await decide(MODEL, state, questions, catalog=catalog_with(binding), usage_kind="test")
    assert binding.calls == []


async def test_missing_api_key_fails_through_require_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FAKE_API_KEY", raising=False)
    binding = FakeBinding(_good())

    def build(_ctx: object) -> FakeBinding:
        require_key("FAKE_API_KEY")
        return binding

    catalog = catalog_with(binding, build=build)  # pyright: ignore[reportArgumentType]
    with pytest.raises(DecisionKeyMissingError, match="FAKE_API_KEY not set") as excinfo:
        await decide(MODEL, "hello", _QUESTIONS, catalog=catalog, usage_kind="test")
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert not isinstance(excinfo.value.__cause__, DecisionKeyMissingError)
    assert binding.calls == []


async def test_a_build_failure_with_the_key_present_propagates_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_API_KEY", "present")

    def build(_ctx: object) -> FakeBinding:
        raise RuntimeError("real bug in build")

    catalog = catalog_with(FakeBinding(_good()), build=build)  # pyright: ignore[reportArgumentType]
    with pytest.raises(RuntimeError, match="real bug in build") as excinfo:
        await decide(MODEL, "hello", _QUESTIONS, catalog=catalog, usage_kind="test")
    assert not isinstance(excinfo.value, DecisionKeyMissingError)


def _bad(mutate: dict[str, Decision]) -> dict[str, Decision]:
    return {**_good(), **mutate}


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        (
            {"spam": Decision(top="true", probabilities={"true": 1.0})},
            "missing \\['false'\\]",
        ),
        (
            {"team": Decision("billing", {"billing": 0.5, "tech": 0.4, "ops": 0.1}, 0.5)},
            "unknown \\['ops'\\]",
        ),
        (
            {"spam": Decision(top="true", probabilities={"true": 0.5, "false": 0.2})},
            "sum to 0.7000",
        ),
        (
            {"spam": Decision(top="false", probabilities={"true": 0.9, "false": 0.1})},
            "not the most probable",
        ),
        (
            {"spam": Decision(top="true", probabilities={"true": 1.4, "false": -0.4})},
            "outside \\[0, 1\\]",
        ),
        (
            {"team": Decision("billing", {"billing": 0.7, "tech": 0.3}, confidence=None)},
            "confidence",
        ),
        (
            {"sev": Decision("mid", {"low": 0.1, "mid": 0.7, "high": 0.2}, 0.5, score=2.5)},
            "outside 0..2",
        ),
    ],
)
async def test_an_invalid_distribution_is_rejected(
    mutation: dict[str, Decision], match: str
) -> None:
    catalog = catalog_with(FakeBinding(_bad(mutation)))
    with pytest.raises(DecisionResponseError, match=match):
        await decide(MODEL, "hello", _QUESTIONS, catalog=catalog, usage_kind="test")


async def test_missing_or_extra_answers_are_rejected() -> None:
    answers = _good()
    del answers["sev"]
    answers["ghost"] = answers["spam"]
    catalog = catalog_with(FakeBinding(answers))

    with pytest.raises(DecisionResponseError, match="missing \\['sev'\\], unknown \\['ghost'\\]"):
        await decide(MODEL, "hello", _QUESTIONS, catalog=catalog, usage_kind="test")


async def test_each_call_writes_one_priced_usage_row_with_no_output_tokens(
    loguru_records: list[dict[str, Any]],
) -> None:
    catalog = catalog_with(FakeBinding(_good(), tokens=2_000_000))

    await decide(
        MODEL,
        "hello",
        _QUESTIONS,
        catalog=catalog,
        usage_kind="injection_check",
        source="web.fetch",
        for_agent_id=7,
    )

    [record] = [r for r in loguru_records if r["extra"].get("event") == "llm_usage"]
    extra = record["extra"]
    assert extra["model"] == MODEL
    assert extra["in_total"] == 2_000_000
    assert extra["out_total"] == 0
    assert extra["usage_kind"] == "injection_check"
    assert extra["source"] == "web.fetch"
    assert extra["agent_id"] == 7
    assert extra["cost_usd"] == pytest.approx(2.0)
    assert "unpriced" not in extra


async def test_usage_is_still_written_when_the_response_proves_invalid(
    loguru_records: list[dict[str, Any]],
) -> None:
    class Failing:
        async def decide(self, state: str, questions: Any) -> Any:
            raise DecisionResponseError("bad body", input_tokens=500)

    with pytest.raises(DecisionResponseError, match="bad body"):
        await decide(MODEL, "hello", _QUESTIONS, catalog=catalog_with(Failing()), usage_kind="test")

    [record] = [r for r in loguru_records if r["extra"].get("event") == "llm_usage"]
    assert record["extra"]["in_total"] == 500


async def test_a_provider_failure_without_billing_writes_no_usage(
    loguru_records: list[dict[str, Any]],
) -> None:
    class Failing:
        async def decide(self, state: str, questions: Any) -> Any:
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await decide(MODEL, "hello", _QUESTIONS, catalog=catalog_with(Failing()), usage_kind="test")
    assert not [r for r in loguru_records if r["extra"].get("event") == "llm_usage"]


def test_builder_is_reusable_after_a_rejected_install() -> None:
    builder = CatalogBuilder()
    bad = provider(FakeBinding({}), prefix="no_dash")
    with pytest.raises(Exception, match="must end with"):
        builder.install_decision("p", bad)
    builder.install_decision("p", provider(FakeBinding({})))
    assert MODEL in builder.build().decision_models
