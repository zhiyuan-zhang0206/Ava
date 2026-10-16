from typing import cast

import pytest

from base.packages.text_budget import (
    LengthBudget,
    Level,
    Unit,
    count_words,
    evaluate,
    measure,
)


def test_words_count_english_splits_on_punctuation():
    assert count_words("hello world") == 2
    assert count_words("hello-world") == 2
    assert count_words("snake_case") == 2


def test_words_count_cjk_characters_individually():
    assert count_words("\u4f60\u597d\u4e16\u754c") == 4
    assert count_words("\u4e2d\u6587") == 2


def test_words_count_kana_and_hangul_characters_individually():
    assert count_words("\u3053\u3093\u306b\u3061\u306f") == 5
    assert count_words("\uc548\ub155\ud558\uc138\uc694") == 5


def test_words_count_mixed_scripts_without_merging_runs():
    assert count_words("hello \u4e16\u754c world") == 4
    assert count_words("\u4e16\u754cabc") == 3


def test_words_count_ignores_punctuation_whitespace_and_symbols():
    assert count_words("") == 0
    assert count_words("   ...  ") == 0
    assert count_words("!!!???") == 0
    assert count_words("\U0001f642\U0001f642") == 0


def test_measure_chars_counts_code_points():
    assert measure("abc", Unit.CHARS) == 3
    assert measure("\u4f60\u597d", Unit.CHARS) == 2


def test_measure_lines_counts_splitlines():
    assert measure("a\nb", Unit.LINES) == 2
    assert measure("a\n", Unit.LINES) == 1
    assert measure("", Unit.LINES) == 0


def test_measure_words_matches_count_words():
    assert measure("hello \u4e16\u754c", Unit.WORDS) == count_words("hello \u4e16\u754c") == 3


def test_measure_rejects_an_unknown_unit():
    with pytest.raises(ValueError):
        measure("x", cast(Unit, "characters"))


def test_budget_rejects_negative_limits():
    with pytest.raises(ValueError):
        LengthBudget(Unit.CHARS, hard=-1)
    with pytest.raises(ValueError):
        LengthBudget(Unit.CHARS, soft=-1)


def test_budget_rejects_a_soft_threshold_at_or_above_the_hard_limit():
    with pytest.raises(ValueError):
        LengthBudget(Unit.CHARS, hard=10, soft=10)
    with pytest.raises(ValueError):
        LengthBudget(Unit.CHARS, hard=10, soft=11)


def test_evaluate_reports_ok_at_or_below_every_threshold():
    verdict = evaluate("ab", LengthBudget(Unit.CHARS, hard=3, soft=2))
    assert (verdict.measured, verdict.level, verdict.remaining) == (2, Level.OK, 1)


def test_evaluate_reports_soft_past_the_soft_threshold_including_at_the_hard_limit():
    budget = LengthBudget(Unit.CHARS, hard=3, soft=2)
    assert evaluate("abc", budget).level is Level.SOFT
    assert evaluate("abc", budget).remaining == 0


def test_evaluate_reports_over_past_the_hard_limit_with_negative_remaining():
    verdict = evaluate("abcd", LengthBudget(Unit.CHARS, hard=3))
    assert (verdict.measured, verdict.level, verdict.remaining) == (4, Level.OVER, -1)


def test_evaluate_measures_the_budgets_unit():
    verdict = evaluate("a\nb\nc", LengthBudget(Unit.LINES, soft=2))
    assert (verdict.measured, verdict.level) == (3, Level.SOFT)


def test_a_soft_only_budget_reports_soft_and_no_remaining():
    verdict = evaluate("x" * 11, LengthBudget(Unit.CHARS, soft=10))
    assert (verdict.level, verdict.remaining) == (Level.SOFT, None)


def test_a_budget_with_no_limits_reports_ok():
    verdict = evaluate("x" * 100, LengthBudget(Unit.CHARS))
    assert (verdict.level, verdict.remaining) == (Level.OK, None)


def test_a_zero_hard_limit_disables_only_the_hard_limit():
    budget = LengthBudget(Unit.CHARS, soft=5)
    assert evaluate("abcdef", budget).level is Level.SOFT


def test_a_zero_soft_threshold_disables_only_the_soft_band():
    budget = LengthBudget(Unit.CHARS, hard=5)
    assert evaluate("abcde", budget).level is Level.OK
    assert evaluate("abcdef", budget).level is Level.OVER


def test_evaluate_prefers_over_when_both_thresholds_are_exceeded():
    budget = LengthBudget(Unit.CHARS, hard=3, soft=2)
    verdict = evaluate("abcd", budget)
    assert (verdict.measured, verdict.level, verdict.remaining) == (4, Level.OVER, -1)
