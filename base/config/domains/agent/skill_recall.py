"""Skill recall — AgentSkillRecallSettings.

Before a turn woken by a person's message or a schedule, one decision-model call scores the
agent's whole skill index against the recent conversation and the best matches are offered to the
agent in a system note. Split out of the memory block: it recalls a different pool, with its own
model, threshold and time budget.
"""

from __future__ import annotations

from pydantic import Field

from base.config.base import EnvSettings


class AgentSkillRecallSettings(EnvSettings):
    skill_recall_enabled: bool = Field(
        default=True,
        alias="AVA_SKILL_RECALL",
        description=(
            "Before each turn woken by a message from a person or by a schedule, score every "
            "skill in the agent's index against the recent conversation and name the best "
            "matches in a system note, so a relevant playbook is offered without the agent "
            "having to match its index by hand. A skill the agent already read, was already "
            "offered, or has preloaded is not offered again until the context is compacted. "
            "Off makes no model call."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
            "per_agent": True,
            "lifecycle": "live",
        },
    )

    skill_recall_model: str = Field(
        default="jev-1.13.0",
        alias="AVA_SKILL_RECALL_MODEL",
        description=(
            "Decision model that scores the skill index for skill recall: one request per "
            "inbound-woken turn, one yes/no question per skill. Must be a decision model "
            "(not a chat model). Calibrated 2026-10-11 by benchmark together with the question "
            "wording, the skill description format and the conversation template skill recall "
            "sends, and skill_recall_threshold is a score on this model's scale: recalibrate "
            "before changing any of them."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
            "per_agent": True,
            "lifecycle": "live",
        },
    )

    skill_recall_threshold: float = Field(
        default=0.60,
        gt=0,
        le=1,
        alias="AVA_SKILL_RECALL_THRESHOLD",
        description=(
            "Lowest probability of 'yes' at which a skill is offered. The model's probabilities "
            "are coarse, so this is a cut between 'clearly helps' and everything else rather "
            "than a fine ranking. Calibrated 2026-10-11 with jev-1.13.0 and the current "
            "question wording."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
            "per_agent": True,
            "lifecycle": "live",
        },
    )

    skill_recall_max: int = Field(
        default=2,
        ge=0,
        alias="AVA_SKILL_RECALL_MAX",
        description=(
            "The most skills one recall note offers. Kept small: an offered skill is a "
            "suggestion to spend context on loading its instructions. Ties on the score are "
            "broken by skill identifier, so the choice is deterministic. 0 offers nothing."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
            "per_agent": True,
            "lifecycle": "live",
        },
    )

    skill_recall_deadline_seconds: float = Field(
        default=3.0,
        gt=0,
        alias="AVA_SKILL_RECALL_DEADLINE_SECONDS",
        description=(
            "Wall-clock budget (seconds) for one skill-recall pass. Recall runs before the LLM "
            "on every person- or schedule-woken turn, so a slow decision provider must not hold "
            "the turn: when the deadline expires the turn proceeds with no skill note and the "
            "next one tries again. Independent of memory_recall_deadline_seconds."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
            "per_agent": True,
            "lifecycle": "live",
        },
    )
