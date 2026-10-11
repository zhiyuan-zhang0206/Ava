"""Skill recall at the before_llm edge: offer the skills that fit the request just received.

When a person's message or a schedule wakes the agent, the skill index is scored against the
recent conversation (`agent.graph.recall.skill_recall`) and the best matches are named in one
system note. This module owns the turn-level rules around that call: when it fires, which skills
it may not offer, how long it may take, and the per-window record that keeps a skill from being
offered twice.

A skill is left out of the candidates when the agent has no use for the offer: its body is
already in the window, because the agent read it (`BaseAgentState.skill_reads`, appended by the
exec child at the one point every body read passes through), a recall note already named it
(`SkillRecallState.prompted`), or `skills_to_expand_at_start` preloaded it. The first two
records describe the current context window, so a compaction, which summarizes the note and the
read away, clears them. The reset is keyed on `compact.version` like the SDK reminder's
once-per-compaction categories: it is applied the first time this hook runs after the version
moved, and no skill body can be read in between because an exec only follows an LLM call, which
this hook precedes.

Independent of passive memory recall, which sits at the same edge: separate switch, separate
deadline, and a timeout here never shortens the other's budget.
"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime
from langgraph.types import Overwrite

from agent import state as _state
from agent.graph.prompt.capabilities import recall_candidates
from agent.graph.recall.skill_recall import SkillRecall, recall_skills
from agent.hooks._registry import Hook
from agent.hooks.compact import auto_compact_will_fire
from agent.messages import tail_has_skill_recall_inbound
from agent.state import SkillRecallState
from base.agents.context import AvaContext, agent_id_from_config
from base.log import logger

_LABEL = "skill-recall"


class SkillRecallHook(Hook):
    """before_llm: name the skills that clearly fit the conversation's latest request.

    No-op (returns None) when the feature is off, in an eval run, when the new tail holds no
    person or schedule inbound, when auto-compact would replace messages this same turn
    (the note would be wiped with them), when every skill is already read, offered or
    preloaded, when the call fails or exceeds `skill_recall_deadline_seconds`, or when no
    skill reaches the threshold. After a compaction it also resets the window's record, with
    or without a note.
    """

    async def __call__(
        self,
        state: _state.AgentState,
        runtime: Runtime[AvaContext],
        config: RunnableConfig,
        /,
    ) -> dict[str, Any] | None:
        agent = runtime.context.require_agent()
        if not agent.skill_recall.skill_recall_enabled or agent.sandbox.eval_isolation:
            return None

        version = state.compact.version
        update: dict[str, Any] = {}
        read: set[str] = set(state.skill_reads)
        prompted: set[str] = set(state.skill_recall.prompted)
        if version > state.skill_recall.window:
            # A compaction summarized away the earlier note and the earlier reads.
            read, prompted = set[str](), set[str]()
            update["skill_reads"] = Overwrite([])
            update["skill_recall"] = SkillRecallState(window=version)

        recall = await self._offer(state, runtime, config, excluded=read | prompted)
        if recall is not None:
            update["messages"] = [recall.note]
            update["skill_recall"] = SkillRecallState(
                prompted=prompted | recall.identifiers, window=version
            )
        return update or None

    async def _offer(
        self,
        state: _state.AgentState,
        runtime: Runtime[AvaContext],
        config: RunnableConfig,
        *,
        excluded: set[str],
    ) -> SkillRecall | None:
        if not tail_has_skill_recall_inbound(state.messages):
            return None
        agent = runtime.context.require_agent()
        catalog = runtime.context.require_catalog()
        if auto_compact_will_fire(state, agent, catalog=catalog):
            logger.info(
                "[{label}] {body}",
                label=_LABEL,
                body="defer: auto-compact predicted, skipping skill recall this turn",
                event="skill_recall",
            )
            return None
        candidates = [c for c in recall_candidates(agent.prompt) if c.identifier not in excluded]
        if not candidates:
            return None
        deadline = agent.skill_recall.skill_recall_deadline_seconds
        try:
            return await asyncio.wait_for(
                recall_skills(
                    state.messages,
                    candidates,
                    agent=agent,
                    catalog=catalog,
                    agent_id=agent_id_from_config(config),
                ),
                timeout=deadline,
            )
        except TimeoutError:
            logger.info(
                "[{label}] {body}",
                label=_LABEL,
                body=f"skip: recall exceeded its {deadline:g}s deadline, no skill note this turn",
                event="skill_recall",
            )
            return None
