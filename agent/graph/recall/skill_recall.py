"""Skill recall: offer the skills that clearly fit the conversation's latest request.

The standing `# Capabilities` index asks the agent to match every task against it by hand, and an
index of hundreds of skills is easy to skim past. Before a turn woken by a person's message or a
schedule, one decision-model request scores every candidate skill against the recent conversation
and the best matches are named in a system note, so a fitting playbook is in front of the agent
without it having to find the line.

One request, one question per skill. The conversation is sent once as the state and every skill
adds only its own yes/no question (about 150 tokens each), so the request grows with the catalog.
A catalog too large for the decision model's request limit raises `DecisionInputTooLargeError`;
that is logged and nothing is offered. The state is never truncated and the catalog is never
chunked here: a partial score over some of the skills would offer the best of an arbitrary subset,
and a request split by chunks would multiply the cost of every inbound turn.

The wording of the state preamble and of the question, and the model and threshold in
`AgentSkillRecallSettings`, were calibrated together by the 2026-10-11 benchmark. They are one
unit: changing any of them means recalibrating the threshold.

A removable layer, like passive memory recall: a decision failure of any kind returns `None`
and the turn goes on without the note.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import NamedTuple

from langchain_core.messages import AnyMessage, HumanMessage

from agent.graph.prompt.capabilities import RecallCandidate, index_entry
from agent.graph.recall.conversation_query import build_conversation_query
from agent.messages import NoteTag, system_note_message
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.lm.catalog.decide.call import decide
from base.lm.catalog.decide.types import DecisionError, DecisionInputTooLargeError, YesNo
from base.log import logger

_LABEL = "skill-recall"

# What the decision model is shown as the state: this preamble, then the recent conversation
# (`build_conversation_query`). The model reads the last message as the request to judge.
STATE_PREAMBLE = (
    "Recent conversation of an AI agent (oldest first; the last message is the latest "
    "request or trigger):\n\n"
)

# The calibrated question, asked once per skill. `{identifier}` and `{description}` are the
# skill's display identifier and its index-line description.
QUESTION_TEMPLATE = (
    "Skill `ava.skills.{identifier}` -- {description}\n\n"
    "If the agent loads this skill now and follows it, will that clearly help it handle "
    "the latest request in the conversation?"
)
TRUE_IF = (
    "Yes: the skill's playbook directly applies to what the agent has to do next and "
    "would clearly improve how it does it."
)
FALSE_IF = (
    "No: the skill is unrelated, only loosely related, or the agent can handle the latest "
    "request just as well without it."
)

# Agent-visible framing. Observational, no implementation detail (obeys the SDK docstring
# discipline since the agent reads this text). It stays close to the drift note's framing
# because the lines below it are the same index lines.
_FRAMING = (
    "Skills that match the current conversation, offered automatically. The descriptions "
    "are summaries; load a skill's full instructions with `ava.help(ava.skills.<path>)` "
    "before using it."
)


class SkillRecall(NamedTuple):
    """Result of a skill recall pass.

    note: the system-styled human message to inject.
    identifiers: the display identifiers of the skills it names -- the caller records these
        so the same skill is not offered again this context window.
    """

    note: HumanMessage
    identifiers: set[str]


def skill_questions(candidates: Sequence[RecallCandidate]) -> dict[str, YesNo]:
    """One calibrated yes/no question per candidate, keyed by the skill's display identifier."""
    return {
        c.identifier: YesNo(
            question=QUESTION_TEMPLATE.format(identifier=c.identifier, description=c.description),
            true_if=TRUE_IF,
            false_if=FALSE_IF,
        )
        for c in candidates
    }


def pick_skills(scores: Mapping[str, float], *, threshold: float, limit: int) -> list[str]:
    """The identifiers scoring at least `threshold`, best first, at most `limit`.

    The model's probabilities are coarse, so equal scores are common; they are ordered by
    identifier, which makes the choice deterministic."""
    qualified = [identifier for identifier, score in scores.items() if score >= threshold]
    qualified.sort(key=lambda identifier: (-scores[identifier], identifier))
    return qualified[:limit]


def render_note(candidates: Sequence[RecallCandidate]) -> HumanMessage:
    """The system-styled note naming `candidates`, one index line each."""
    lines = [index_entry(c.identifier, c.description) for c in candidates]
    return system_note_message(
        content=f"{_FRAMING}\n\n" + "\n".join(lines),
        tag=NoteTag.SKILL_RECALL,
        created_at=datetime.now(UTC),
    )


async def recall_skills(
    messages: Sequence[AnyMessage],
    candidates: Sequence[RecallCandidate],
    *,
    agent: AgentSlices,
    catalog: ModelCatalog,
    agent_id: int,
) -> SkillRecall | None:
    """Score `candidates` against the recent conversation and render the best matches as a
    note to inject, or `None` when there is nothing to offer.

    `None` when: the conversation yields no query, there are no candidates, no candidate
    reaches the threshold, or the decision call failed. A failure is logged, never raised: the
    caller is a before_llm hook, so an exception here would end the agent process rather than
    just the recall. The caller applies the deadline and has already removed the skills that
    must not be offered (read, offered, preloaded)."""
    settings = agent.skill_recall
    query = build_conversation_query(messages)
    if not query or not candidates:
        return None
    by_identifier = {c.identifier: c for c in candidates}
    try:
        decisions = await decide(
            settings.skill_recall_model,
            STATE_PREAMBLE + query,
            skill_questions(candidates),
            catalog=catalog,
            usage_kind="skill_recall",
            for_agent_id=agent_id,
            timeout=settings.skill_recall_deadline_seconds,
        )
    except DecisionInputTooLargeError as exc:
        # Not a transient failure: this agent's catalog does not fit one request, so recall
        # stays off for it until the catalog shrinks or the model's limit grows.
        logger.warning(
            "[{label}] {body}",
            label=_LABEL,
            body=f"skip: {len(candidates)} candidate skills do not fit one request ({exc})",
            event="skill_recall",
        )
        return None
    except DecisionError as exc:
        logger.warning(
            "[{label}] {body}",
            label=_LABEL,
            body=f"skip: decision call failed ({type(exc).__name__}: {exc})",
            event="skill_recall",
        )
        return None
    scores = {identifier: d.probability("true") for identifier, d in decisions.items()}
    picked = pick_skills(
        scores, threshold=settings.skill_recall_threshold, limit=settings.skill_recall_max
    )
    logger.info(
        "[{label}] {body}",
        label=_LABEL,
        body=f"scored {len(candidates)} skills, offering {picked or 'none'}",
        event="skill_recall",
        candidates=len(candidates),
        offered=picked,
    )
    if not picked:
        return None
    return SkillRecall(
        note=render_note([by_identifier[identifier] for identifier in picked]),
        identifiers=set(picked),
    )
