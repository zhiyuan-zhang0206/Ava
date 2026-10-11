"""Question and Decision value types fail fast on malformed construction."""

from __future__ import annotations

import pytest

from base.lm.catalog.decide.types import (
    Choice,
    Decision,
    DecisionModelSpec,
    Score,
    YesNo,
    estimate_tokens,
    question_text,
)


def test_yes_no_requires_all_three_texts() -> None:
    assert YesNo("Is it spam?", "it is spam", "it is not").outcomes == ("true", "false")
    with pytest.raises(ValueError, match="true_if"):
        YesNo("Is it spam?", " ", "it is not")


def test_choice_needs_two_to_255_described_options() -> None:
    assert Choice("Which?", {"a": "first", "b": "second"}).outcomes == ("a", "b")
    with pytest.raises(ValueError, match=r"2\.\.255"):
        Choice("Which?", {"a": "only one"})
    with pytest.raises(ValueError, match=r"2\.\.255"):
        Choice("Which?", {f"o{i}": "d" for i in range(256)})
    with pytest.raises(ValueError, match="description"):
        Choice("Which?", {"a": "first", "b": ""})


def test_score_needs_aligned_unique_levels_within_bounds() -> None:
    score = Score("How bad?", ("low", "high"), ("fine", "broken"))
    assert score.outcomes == ("low", "high")
    with pytest.raises(ValueError, match="one description per level"):
        Score("How bad?", ("low", "high"), ("fine",))
    with pytest.raises(ValueError, match="unique"):
        Score("How bad?", ("low", "low"), ("a", "b"))
    with pytest.raises(ValueError, match=r"2\.\.10"):
        Score("How bad?", tuple(str(i) for i in range(11)), tuple("d" * 11))
    with pytest.raises(ValueError, match=r"2\.\.10"):
        Score("How bad?", ("only",), ("one",))


def test_decision_probability_rejects_unknown_outcome() -> None:
    decision = Decision(top="true", probabilities={"true": 0.9, "false": 0.1})
    assert decision.probability("true") == 0.9
    with pytest.raises(KeyError, match="maybe"):
        decision.probability("maybe")


def test_model_spec_limits_are_consistent() -> None:
    assert DecisionModelSpec(context_window=10).request_limit == 10
    assert DecisionModelSpec(context_window=10, max_request_tokens=20).request_limit == 20
    with pytest.raises(ValueError, match="positive"):
        DecisionModelSpec(context_window=0)
    with pytest.raises(ValueError, match="max_request_tokens"):
        DecisionModelSpec(context_window=10, max_request_tokens=5)


def test_token_estimate_counts_non_ascii_characters_individually() -> None:
    assert estimate_tokens("abcd" * 10) == 10
    assert estimate_tokens("\u4f60\u597d\u4e16\u754c") == 4
    assert estimate_tokens("") == 0


def test_question_text_carries_every_string() -> None:
    text = question_text(Choice("Which one?", {"alpha": "first thing"}.copy() | {"beta": "second"}))
    for fragment in ("Which one?", "alpha", "first thing", "beta", "second"):
        assert fragment in text
