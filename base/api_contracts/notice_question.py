"""One immutable notice question and its numbered, validated user answer.

Option order is the numbering contract for every UI, history and inbound.
Other occupies the next number; mentioning a number in text never selects it.
"""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter, model_validator

NonBlank = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1)]


class QuestionType(StrEnum):
    SINGLE_SELECT = "single_select"
    MULTI_SELECT = "multi_select"
    TEXT = "text"


class NoticeOption(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: NonBlank
    label: NonBlank
    description: NonBlank | None = None


class ChoiceQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal[QuestionType.SINGLE_SELECT, QuestionType.MULTI_SELECT]
    options: Annotated[list[NoticeOption], Field(min_length=1, max_length=20)]
    allow_other: Annotated[bool, Field(strict=True)] = False

    @model_validator(mode="after")
    def unique_options(self) -> "ChoiceQuestion":
        if len({option.id for option in self.options}) != len(self.options):
            raise ValueError("option ids must be unique")
        return self


class TextQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal[QuestionType.TEXT]


NoticeQuestion = Annotated[ChoiceQuestion | TextQuestion, Field(discriminator="type")]


class NoticeAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    option_ids: list[NonBlank] = Field(default_factory=list)
    other_text: NonBlank | None = None
    text: NonBlank | None = None
    comment: NonBlank | None = None


def parse_question(value: object) -> NoticeQuestion:
    """Validate raw SDK, persisted or wire input; unknown types fail immediately."""
    return TypeAdapter[ChoiceQuestion | TextQuestion](NoticeQuestion).validate_python(value)


def validate_answer(question: NoticeQuestion, answer: NoticeAnswer) -> NoticeAnswer:
    """Reject invalid answers and canonicalize selections into the frozen option order."""
    if isinstance(question, TextQuestion):
        if answer.text is None or answer.option_ids or answer.other_text is not None:
            raise ValueError("text questions require text and cannot select options or Other")
        return answer
    _validate_choice_fields(question, answer)
    selected_count = len(answer.option_ids) + int(answer.other_text is not None)
    if question.type == QuestionType.SINGLE_SELECT and selected_count != 1:
        raise ValueError("single-select questions require exactly one option or Other")
    if selected_count == 0:
        raise ValueError("select at least one option or provide Other")
    return answer.model_copy(
        update={"option_ids": [o.id for o in question.options if o.id in answer.option_ids]}
    )


def _validate_choice_fields(question: ChoiceQuestion, answer: NoticeAnswer) -> None:
    if answer.text is not None:
        raise ValueError("choice questions cannot contain a text answer; use Other or comment")
    if len(set(answer.option_ids)) != len(answer.option_ids):
        raise ValueError("selected option ids must be unique")
    valid_ids = {option.id for option in question.options}
    if not set(answer.option_ids) <= valid_ids:
        raise ValueError("answer contains an unknown option id")
    if answer.other_text is not None and not question.allow_other:
        raise ValueError("this question does not allow Other")


def numbered_options(question: ChoiceQuestion) -> str:
    """Render the same one-based option mapping for terminal, IM and agent inbounds."""
    lines: list[str] = []
    for number, option in enumerate(question.options, start=1):
        line = f"{number}. {option.label}"
        if option.description is not None:
            line += f" — {option.description}"
        lines.append(line)
    if question.allow_other:
        lines.append(f"{len(question.options) + 1}. Other")
    return "\n".join(lines)


def question_instructions(question: NoticeQuestion) -> str:
    """Explain the deliberately small numbered-reply grammar for text-only clients."""
    if isinstance(question, TextQuestion):
        return "Reply with your answer."
    mode = (
        "one number"
        if question.type == QuestionType.SINGLE_SELECT
        else "numbers separated by commas"
    )
    return (
        f"{numbered_options(question)}\n\nReply with {mode} on the first line. "
        + (
            "If selecting Other, put its answer on the following lines; "
            if question.allow_other
            else ""
        )
        + "otherwise following lines are an optional note."
    )


def parse_numbered_answer(question: NoticeQuestion, reply: str) -> NoticeAnswer:
    """Adapt a CLI/IM reply to the same answer contract as the Web form."""
    if isinstance(question, TextQuestion):
        return validate_answer(question, NoticeAnswer(text=reply))
    first, _, following = reply.strip().partition("\n")
    numbers = _selected_numbers(question, first)
    other = len(question.options) + 1 in numbers
    if other and not following.strip():
        raise ValueError("selecting Other requires text on the following lines")
    return validate_answer(
        question,
        NoticeAnswer(
            option_ids=[question.options[n - 1].id for n in numbers if n <= len(question.options)],
            other_text=following if other else None,
            comment=following if following.strip() and not other else None,
        ),
    )


def _selected_numbers(question: ChoiceQuestion, first: str) -> list[int]:
    try:
        numbers = [int(token.strip()) for token in first.split(",")]
    except ValueError as exc:
        raise ValueError(
            "reply first line must contain option numbers separated by commas"
        ) from exc
    limit = len(question.options) + int(question.allow_other)
    if any(number < 1 or number > limit for number in numbers):
        raise ValueError("reply contains an unknown option number")
    if len(set(numbers)) != len(numbers):
        raise ValueError("selected option numbers must be unique")
    return numbers


def answer_reply(question: NoticeQuestion, answer: NoticeAnswer) -> str:
    """Build a readable projection with every numbered reference preserved."""
    answer = validate_answer(question, answer)
    if isinstance(question, TextQuestion):
        lines = [f"Answer: {answer.text}"]
    else:
        selected = [str(i) for i, o in enumerate(question.options, 1) if o.id in answer.option_ids]
        if answer.other_text is not None:
            selected.append(str(len(question.options) + 1))
        lines = ["Options:", numbered_options(question), "", f"Selected: {', '.join(selected)}"]
        if answer.other_text is not None:
            lines.append(f"Other ({len(question.options) + 1}): {answer.other_text}")
    if answer.comment is not None:
        lines.append(f"Additional note: {answer.comment}")
    return "\n".join(lines)
