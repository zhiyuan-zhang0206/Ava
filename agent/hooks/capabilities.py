"""The drift check that keeps the `# Capabilities` index honest between builds.

`# Capabilities` is rendered into the SystemMessage, and `init_context` lays that
message down only when a context window is established — an agent's first wake,
and the turn after each compaction. `ava.skills` underneath it is an uncached
filesystem scan. So the index is a snapshot of something live, and a skill
installed mid-window is reachable by name while absent from the one listing the
prompt tells the agent to match every task against — until a compaction happens
to rebuild the prompt.

The gap closes against the snapshot `init_context` records (`state.capabilities`):
before each LLM call — the moment the agent reads its index — the live index
membership is diffed against that record, and whatever appeared since is named in
one system note using the same line shape the index uses. Drift is the trigger,
not a timer, and the snapshot advances with the note, so one install produces one
note regardless of who installed it: this agent's own code, another agent on the
box, or an operator running `ava skill install`.

A narrowed agent (`skills_to_inject_into_system_prompt` naming specific skills
rather than `*`) is served by the same diff without a special case — membership
is whatever the configured list resolves to, so a name that resolved to nothing
at build time and resolves now is drift, while a skill outside the list never
becomes drift and the narrowing holds.

The note is suppressed on two turns: a container/eval run (no `ops_pool`, the
same signal `init_context` reduces its head by), and any turn a compaction is
about to replace the window — see the defer in `__call__`, which is what keeps
this hook from destroying the compaction it rides along with.
"""

from __future__ import annotations

from datetime import UTC, datetime

from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from agent import state as _state
from agent.graph._capabilities import index_drift, new_skills_note_text
from agent.hooks._registry import Hook
from agent.hooks.compact import auto_compact_will_fire
from agent.messages import NoteTag, system_note_message
from agent.state import CapabilitiesState
from shared.context import AvaContext
from shared.log import logger


class _NewlyInstalledSkillsHook(Hook):
    """before_llm: name the skills that appeared since the standing index was
    built, and advance the snapshot so each one is named once.

    No-op (returns None) on the common path where nothing changed — one skill
    scan per turn, which is filesystem-only and orders of magnitude under the
    LLM call it precedes.
    """

    async def __call__(
        self,
        state: _state.AgentState,
        runtime: Runtime[AvaContext],
        _config: RunnableConfig,
        /,
    ) -> dict | None:
        if runtime.context.ops_pool is None:
            # Container mode (eval harness) — the same signal `init_context` uses
            # to reduce its head to the system prompt alone. An eval's context is
            # deterministic by construction, and a note whose trigger is whatever
            # the host filesystem gained mid-run is exactly what that excludes.
            return None

        if auto_compact_will_fire(state):
            # Not politeness — the only safe move. Compaction writes
            # `RemoveMessage(REMOVE_ALL)` on this same `messages` channel and
            # detours to `init_context`, whose sole trigger is an EMPTY channel.
            # `add_messages` applies the wipe and THEN the append, so a note
            # written in this pass is the one thing that survives it:
            # `init_context` would read that single note as an intact history,
            # take the pass-through branch, drop the parked summary, and never
            # lay down the SystemMessage — the agent would run the whole next
            # window on one orphan note. Staying quiet costs nothing, because
            # the compaction rebuilds `# Capabilities` from the catalog that
            # now contains these skills and re-snapshots.
            logger.info(
                "[capabilities] defer: auto-compact predicted, skipping the drift note this turn"
            )
            return None

        known = state.capabilities.indexed
        drift = index_drift(known if known is not None else set())
        if known is None:
            # No snapshot for this window — a checkpoint written before the
            # field existed. Adopt the live catalog as the baseline silently:
            # see `CapabilitiesState` for why announcing it instead would be
            # worse than saying nothing.
            return {"capabilities": CapabilitiesState(indexed=drift.identifiers)}
        if not drift.added:
            return None

        import ava

        added = [ava.skills.identifier(s) for s in drift.added]
        logger.info(
            "[capabilities] {} skill(s) installed since the index was built: {}",
            len(added),
            ", ".join(added),
        )
        # The note rides the `messages` channel, whose add_messages reducer lets
        # a sibling hook co-write it without the runner's clash guard firing. A
        # compaction is the sibling that matters, and merging with it is NOT
        # safe — hence the defer above, which is why control only reaches here on
        # a turn that keeps its history.
        return {
            "messages": [
                system_note_message(
                    content=new_skills_note_text(drift.added),
                    tag=NoteTag.NEW_SKILLS,
                    created_at=datetime.now(UTC),
                )
            ],
            "capabilities": CapabilitiesState(indexed=drift.identifiers),
        }


# Module-level singleton — the registered instance, so its identity is stable
# across the re-registration each graph build performs.
_newly_installed_skills = _NewlyInstalledSkillsHook()


def register_capabilities_hooks() -> None:
    """Register the built-in capability-index drift before_llm hook.

    Called once at graph build time (from `build_graph`), after repair and
    compact: repair guards the payload, compact may replace the whole history,
    and this appends to whatever survives them. Unconditional — an index that
    silently under-reports is a correctness problem, not an optional layer."""
    from agent.hooks import register_before_llm

    register_before_llm(_newly_installed_skills)
