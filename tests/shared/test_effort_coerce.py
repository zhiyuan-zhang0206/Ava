"""`shared/lm/_effort.coerce_effort` unit tests — the one public effort contract.

The entry-point tests (test_web / test_understand) cover the same contract
through the public APIs; these pin the shared function directly, including the
`example` message parameter that keeps each API's own wording.
"""

from __future__ import annotations

import pytest

from shared.lm._effort import ReasoningEffort, coerce_effort


def test_coerce_effort_passes_none_through() -> None:
    """None means "keep the settings default" on the fetch path — untouched."""
    assert coerce_effort(None, example="x") is None


def test_coerce_effort_normalizes_literals_and_members() -> None:
    """Both spellings of a level — the enum member and its literal string —
    normalize to the same enum value."""
    assert coerce_effort("low", example="x") == ReasoningEffort.LOW
    assert coerce_effort(ReasoningEffort.LOW, example="x") == ReasoningEffort.LOW
    assert coerce_effort("max", example="x") == ReasoningEffort.MAX
    assert coerce_effort(ReasoningEffort.XHIGH, example="x") == ReasoningEffort.XHIGH


def test_coerce_effort_rejects_non_string_type() -> None:
    with pytest.raises(TypeError, match="effort"):
        coerce_effort(3, example="x")  # type: ignore[arg-type]


def test_coerce_effort_rejects_unknown_values() -> None:
    """Unknown strings fail fast; `minimal` is gemini-internal (a
    thinking_level, not a public effort level) and is rejected here."""
    with pytest.raises(ValueError, match="effort"):
        coerce_effort("ultra", example="x")
    with pytest.raises(ValueError, match="effort"):
        coerce_effort("minimal", example="x")


def test_coerce_effort_message_names_the_calling_api() -> None:
    with pytest.raises(ValueError, match=r"ava\.web\.fetch\(targets, effort='high'\)"):
        coerce_effort("ultra", example="ava.web.fetch(targets, effort='high')")
    with pytest.raises(TypeError, match=r"ava\.understand\(targets, effort='low'\)"):
        coerce_effort(3, example="ava.understand(targets, effort='low')")  # type: ignore[arg-type]
