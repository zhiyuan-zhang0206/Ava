"""Bind a submitted answer to the locked notice's immutable question."""

from typing import NamedTuple

from fastapi import HTTPException

from base.api_contracts.notice_question import (
    NoticeAnswer,
    NoticeQuestion,
    answer_reply,
    parse_numbered_answer,
    parse_question,
    validate_answer,
)
from gateway.agents.schemas import ResolveNoticeIn


class ResolutionAnswer(NamedTuple):
    reply: str | None
    answer: NoticeAnswer | None


def resolution_answer(body: ResolveNoticeIn, raw_question: object) -> ResolutionAnswer:
    """Adapt text-only clients and validate against the authoritative stored options."""
    if raw_question is None:
        if body.answer is not None:
            raise HTTPException(status_code=422, detail="this notice has no structured question")
        return ResolutionAnswer(body.reply, None)
    question = parse_question(raw_question)
    if body.action != "answer":
        return ResolutionAnswer(body.reply, None)
    try:
        answer = _submitted_answer(question, body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ResolutionAnswer(answer_reply(question, answer), answer)


def _submitted_answer(question: NoticeQuestion, body: ResolveNoticeIn) -> NoticeAnswer:
    if body.answer is not None:
        return validate_answer(question, body.answer)
    if body.reply is not None:
        return parse_numbered_answer(question, body.reply)
    raise ValueError("answer requires a structured answer or a numbered text reply")


def resolution_inbound(title: str, action: str, reply: str | None) -> str | None:
    """Render the resolution as notice-system content, never an implicit approval."""
    if reply is not None:
        return f'Re: "{title}"\n\n{reply}'
    if action == "dismiss":
        return f'Re: "{title}"\n\n[This notice was dismissed by the user — no answer will arrive.]'
    return None
