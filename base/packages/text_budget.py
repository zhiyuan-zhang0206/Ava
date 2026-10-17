from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


# meiva:ref base/packages/docs/text-budget.meiva.okf.md#text-units
class Unit(StrEnum):
    CHARS = "chars"
    WORDS = "words"
    LINES = "lines"


_CJK_CHARACTER = re.compile("[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff]")

_WORD_RUN = re.compile(r"[^\W_]+")


# meiva:ref base/packages/docs/text-budget.meiva.okf.md#text-units
def count_words(text: str) -> int:
    without_cjk = _CJK_CHARACTER.sub(" ", text)
    return len(_CJK_CHARACTER.findall(text)) + len(_WORD_RUN.findall(without_cjk))


# meiva:ref base/packages/docs/text-budget.meiva.okf.md#text-units
def measure(text: str, unit: Unit) -> int:
    if unit is Unit.CHARS:
        return len(text)
    if unit is Unit.LINES:
        return len(text.splitlines())
    if unit is Unit.WORDS:
        return count_words(text)
    raise ValueError(f"unknown unit: {unit!r}")


# meiva:ref base/packages/docs/text-budget.meiva.okf.md#length-budget
class Level(StrEnum):
    OK = "ok"
    SOFT = "soft"
    OVER = "over"


# meiva:ref base/packages/docs/text-budget.meiva.okf.md#length-budget
@dataclass(frozen=True)
class LengthBudget:
    unit: Unit
    hard: int = 0
    soft: int = 0

    def __post_init__(self) -> None:
        if self.hard < 0 or self.soft < 0:
            raise ValueError(f"limits cannot be negative: hard={self.hard}, soft={self.soft}")
        if self.hard > 0 and self.soft >= self.hard:
            raise ValueError(
                f"a set soft threshold must be below the hard limit: "
                f"soft={self.soft}, hard={self.hard}"
            )


# meiva:ref base/packages/docs/text-budget.meiva.okf.md#length-budget
@dataclass(frozen=True)
class Verdict:
    measured: int
    budget: LengthBudget
    level: Level

    @property
    def remaining(self) -> int | None:
        if self.budget.hard <= 0:
            return None
        return self.budget.hard - self.measured


# meiva:ref base/packages/docs/text-budget.meiva.okf.md#length-budget
def evaluate(text: str, budget: LengthBudget) -> Verdict:
    measured = measure(text, budget.unit)
    if budget.hard > 0 and measured > budget.hard:
        level = Level.OVER
    elif budget.soft > 0 and measured > budget.soft:
        level = Level.SOFT
    else:
        level = Level.OK
    return Verdict(measured=measured, budget=budget, level=level)
