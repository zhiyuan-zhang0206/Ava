"""Value types of the decision-model primitive.

A decision model answers a fixed set of questions about a piece of text with a probability
distribution and never generates text. It is deliberately separate from chat models: it has no
``ModelSpec``, no ``build_chat_model`` path and no spawn-dropdown entry. Providers implement
``DecisionBinding``; callers use ``base.lm.catalog.decide.call.decide``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

TRUE = "true"
FALSE = "false"

MAX_CHOICE_OPTIONS = 255
MIN_SCORE_LEVELS = 2
MAX_SCORE_LEVELS = 10
# Largest accepted deviation of a distribution's sum from 1. Providers round their
# probabilities to two decimals, so the bound must exceed rounding error across an
# option list.
SUM_TOLERANCE = 0.02


def _require_text(value: object, what: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{what} must be a non-empty string")


@dataclass(frozen=True)
class YesNo:
    """Is a condition true? ``true_if`` / ``false_if`` define the two outcomes."""

    question: str
    true_if: str
    false_if: str

    def __post_init__(self) -> None:
        _require_text(self.question, "YesNo.question")
        _require_text(self.true_if, "YesNo.true_if")
        _require_text(self.false_if, "YesNo.false_if")

    @property
    def outcomes(self) -> tuple[str, ...]:
        return (TRUE, FALSE)


@dataclass(frozen=True)
class Choice:
    """Pick exactly one option. ``options`` maps each option name to its description."""

    question: str
    options: Mapping[str, str]

    def __post_init__(self) -> None:
        _require_text(self.question, "Choice.question")
        if not 2 <= len(self.options) <= MAX_CHOICE_OPTIONS:
            raise ValueError(
                f"Choice needs 2..{MAX_CHOICE_OPTIONS} options, got {len(self.options)}"
            )
        for name, description in self.options.items():
            _require_text(name, "Choice option name")
            _require_text(description, f"Choice option {name!r} description")

    @property
    def outcomes(self) -> tuple[str, ...]:
        return tuple(self.options)


@dataclass(frozen=True)
class Score:
    """Rate against an ordered scale, lowest level first.

    ``scale`` holds the unique level labels and ``rubric`` the description of each level at
    the same position. Levels are numbered from 0 by position.
    """

    question: str
    scale: tuple[str, ...]
    rubric: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_text(self.question, "Score.question")
        if not MIN_SCORE_LEVELS <= len(self.scale) <= MAX_SCORE_LEVELS:
            raise ValueError(
                f"Score needs {MIN_SCORE_LEVELS}..{MAX_SCORE_LEVELS} levels, got {len(self.scale)}"
            )
        if len(self.rubric) != len(self.scale):
            raise ValueError(
                f"Score.rubric needs one description per level: {len(self.scale)} levels, "
                f"{len(self.rubric)} descriptions"
            )
        if len(set(self.scale)) != len(self.scale):
            raise ValueError("Score.scale labels must be unique")
        for label, description in zip(self.scale, self.rubric, strict=True):
            _require_text(label, "Score level label")
            _require_text(description, f"Score level {label!r} description")

    @property
    def outcomes(self) -> tuple[str, ...]:
        return self.scale


Question = YesNo | Choice | Score


@dataclass(frozen=True)
class Decision:
    """The answer to one question.

    ``probabilities`` is the complete distribution over the question's outcomes: ``"true"`` /
    ``"false"`` for ``YesNo``, the option names for ``Choice``, the scale labels for ``Score``.
    ``top`` is the most probable outcome. ``confidence`` (0..1) is set for ``Choice`` and
    ``Score`` only; ``score`` is the probability-weighted 0-based level index, set for ``Score``
    only.
    """

    top: str
    probabilities: Mapping[str, float]
    confidence: float | None = None
    score: float | None = None

    def probability(self, outcome: str) -> float:
        """Probability of ``outcome``; an outcome the question does not have raises."""
        if outcome not in self.probabilities:
            raise KeyError(f"unknown outcome {outcome!r}; known: {sorted(self.probabilities)}")
        return self.probabilities[outcome]


@dataclass(frozen=True)
class DecisionResult:
    """What a binding returns: the decisions and the input tokens the provider billed."""

    decisions: Mapping[str, Decision]
    input_tokens: int


@dataclass(frozen=True)
class DecisionModelSpec:
    """Static facts of one decision model.

    ``context_window`` bounds the state plus the longest single question;
    ``max_request_tokens`` bounds the state plus all questions together (``None`` = the same as
    ``context_window``). ``media_types`` lists non-text inputs the model accepts natively; text
    is always accepted, and ``decide()`` currently passes text only.
    """

    context_window: int
    media_types: frozenset[str] = frozenset()
    max_request_tokens: int | None = None

    def __post_init__(self) -> None:
        if self.context_window <= 0:
            raise ValueError("DecisionModelSpec.context_window must be positive")
        if self.max_request_tokens is not None and self.max_request_tokens < self.context_window:
            raise ValueError("DecisionModelSpec.max_request_tokens must be >= context_window")

    @property
    def request_limit(self) -> int:
        return self.max_request_tokens or self.context_window


class DecisionBinding(Protocol):
    """A provider's client for one decision model."""

    async def decide(self, state: str, questions: Mapping[str, Question]) -> DecisionResult:
        """Answer every question about ``state``; raise ``DecisionError`` on any failure."""
        ...


class DecisionError(RuntimeError):
    """A decision call failed. Subclasses say how."""


class UnknownDecisionModelError(DecisionError):
    """The model id is not in the catalog's decision table."""


class DecisionInputTooLargeError(DecisionError):
    """The estimated input exceeds the model's limits; nothing was sent. Callers truncate or chunk."""


class DecisionRequestError(DecisionError):
    """The provider request failed (transport error or non-success status)."""


class DecisionResponseError(DecisionError):
    """The provider answered with a malformed or inconsistent response.

    ``input_tokens`` is the billed count when the response carried one before it proved bad, so
    the call is still accounted.
    """

    def __init__(self, message: str, *, input_tokens: int | None = None) -> None:
        super().__init__(message)
        self.input_tokens = input_tokens


def estimate_tokens(text: str) -> int:
    """Conservative token estimate: 4 ASCII characters per token, 1 token per other character.

    Used only for the pre-flight size check; the provider reports the billed count.
    """
    ascii_chars = len(text.encode("ascii", "ignore"))
    return math.ceil(ascii_chars / 4) + (len(text) - ascii_chars)


def question_text(question: Question) -> str:
    """Every string of ``question``, for size estimation."""
    match question:
        case YesNo():
            return "\n".join((question.question, question.true_if, question.false_if))
        case Choice():
            parts = [question.question]
            for name, description in question.options.items():
                parts.extend((name, description))
            return "\n".join(parts)
        case Score():
            return "\n".join((question.question, *question.scale, *question.rubric))
