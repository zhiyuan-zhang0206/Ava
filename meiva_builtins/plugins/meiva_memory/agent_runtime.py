"""Agent-runtime face of the meiva_memory plugin — context notes, prompt section, recall hook, write check.

Loaded only in the agent process: the plugin loader (`base.packages.plugins.loading`) imports this module after
`plugin.py` on the full path (host boot / graph build). The plugin's SDK
**surface** — the `meiva.memory` namespace (PATH / PERSONAL_PATH / search) and the
two `files.*` wraps that record written notes — lives in `plugin.py` and loads
in agent-launched children too (task #3633): exec code uses `meiva.memory`
heavily, but never the context-note registry, the recall hook or the check.
"""

from __future__ import annotations

import asyncio
from collections.abc import Collection
from datetime import UTC, datetime
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime
from langgraph.types import Overwrite

from agent.graph.hooks.compact import auto_compact_will_fire
from agent.graph.prompt.context_notes import (
    RANK_CLUSTER_MEMORY,
    RANK_INHERITED_MEMORY,
    RANK_PER_AGENT_MEMORY,
)
from agent.graph.recall.memory_recall import PassiveRecall, passive_memory_recall
from agent.hooks import Hook
from agent.messages import NoteTag, system_note_message, tail_has_recallable_inbound
from agent.state import AgentState, MemoryState, PluginStateHandle
from base.agents.context import MeivaContext
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.log import logger
from base.packages.plugins.extensions import ContextNote, PluginContributions
from base.paths import memory_dir, workspace_dir

from ._state import MemoryWriteState
from .inherit import inherited_memory_note
from .notes import memory_index_note, per_agent_memory_note
from .write_checks import check_written_notes, render_reminder


def state_handle() -> PluginStateHandle[MemoryWriteState]:
    """The typed handle over this plugin's state — a view over the turn's state slot, built per use."""
    return PluginStateHandle(MemoryWriteState, "meiva_memory")


# The two memory indexes are declared in `contribute()` below, so `init_context` lays them down
# with the framework's notes whenever a window is established. The ranks pin them into the
# reading order the framework documents (see the rank scale in `context_notes.py`): the shared
# index right after the exec-timeout note, the per-agent index right after the agent-id note.
# Disabling this plugin means neither is ever declared, and a window is laid down with no memory
# notes in it — matching an agent that has no memory stores.
# The shared index is NOT `on_fork`: the pool is cluster-wide, so the copy
# a fork inherits with the source's history is the same content the graft would
# add — grafting it duplicates the index in the forked agent's window
# (issue #1320). Same reasoning as the framework's timezone note. The per-agent
# index IS `on_fork`: it names the source agent's store, which the inherited
# history renders wrong for the new agent — `_handle_fork` strips the inherited
# copy before grafting the new agent's own. The inherited-memory note is
# `on_fork` for the same reason: the source's history carries the blocks read
# from the SOURCE's chain, which is not the new agent's chain.


# ── Memory discipline (system prompt section) ──────────────────────────

# The write discipline both stores share. It is fixed text — the same bytes for
# every agent on every turn — so it belongs in the system prompt, not in a note
# beside the indexes: an index carries content that changes as the agent writes,
# this does not.
_DISCIPLINE = """\
# Remembering across sessions

Your memory — two stores, one discipline.

- Shared pool (meiva.memory.PATH): notes every agent can read. Restrained by
  design: only reusable rules (each with a Why and a How to apply), facts many
  agents repeatedly reach for, and user rulings. Events stay out by default —
  git history already carries them. Judge each note by one question: "would a
  new agent reading this in a year behave differently?" If not, it does not
  belong in the pool.
- Personal (memory/ in your workspace): your own durable state, maintained by
  you alone. Verbose is fine here: process details, half-formed understanding,
  notes of uncertain future value — keep them personal until they earn a place
  in the pool.

Unsure which store? Personal — the pool is for rules other agents will use,
not your working details.

Each store puts its index in front of you when a session starts and after a
compact; the entries themselves you read on demand.

What makes a memory worth writing — applicable, durable, legible:

- applicable: it changes what you do later — a correction you were given, a
  stated preference, a non-obvious procedure or invariant. Not what the code,
  repo docs, AGENTS.md, or a fresh lookup already tells you.
- durable: it holds in more than one session, written as a reusable rule ("this
  service rate-limits above 3 retries, so more retries make it worse"), not as
  an episode ("set retries to 3 here"). Live status ("in flight", "awaiting
  review"), branch and PR inventories, and who currently holds which role are
  cluster state, not memory.
- legible: readable months later by an agent that was not there — one topic per
  file, full sentences, the why and not only the what. No bare ticket ids, no
  "the fix".

You must write or update memory when:

- Someone corrects you, however it is phrased: a "do it this way instead" edit,
  a pushback, or a skeptical question ("won't that break X?"). Answer it, then
  record the preference behind it — answering is not saving. Scope words ("for
  now", "in this change") mark a one-off to follow, not a rule to save.
- You learn something durable about your environment: a path, a port, a quirk of
  this machine or cluster that a fresh lookup would not reveal.

Write it in the same turn you engage the correction, before you treat that turn
as finished.

Memory maintenance is an important standing duty — not a side chore you
fit in after your real task. When you notice a memory that is stale or wrong,
update it first, before continuing whatever you were doing. Don't "notice and
move on", and don't wait for consolidation to sweep it up; a stale note keeps
misleading every later agent until it is corrected.

Correcting stale memory is an obligation, not an option:

- When a memory — yours, a peer's, or the shared index's Setup section —
  contradicts a self-verifying fact (a branch that no longer exists, a path
  that moved, a setting that changed), fix it in the same turn you notice it:
  edit the entry in place — a correction replaces the stale note, never a
  second note beside it: appending a newer note beside the stale claim leaves
  the contradiction in front of every agent; only the correction removes it.
- The shared index's Setup section (repos, key facts, current tasks) has no
  single owner. Whoever spots an outdated claim there is responsible for
  correcting it — or reporting it to the Memory Arbiter when unsure.
- Not sure how to fix it? Ask the owning agent — but uncertainty is not an
  excuse to leave it alone: at minimum mark it "possibly stale — needs
  confirmation" so later readers do not trust it.
- Before writing, search first: check the index and the pool for an entry
  that already covers the ground. Update it if it exists; create only when
  nothing does. Deleting an entry that turns out to be wrong is a valid write.

Every note carries exactly one type tag: type/user (who the user is),
type/feedback (how you should work, with the reason), type/project (ongoing
work, goals, constraints), type/reference (pointers to external resources),
type/env (machine and cluster facts), type/role (an agent's role and its
boundaries). Lead a type/feedback or type/project body with the rule or fact,
then a "Why:" line and a "How to apply:" line.

A memory is a claim, not a source of truth. Your sources differ in how they can
be checked:

- self-verifying: running code, meiva cluster status, the database, the
  filesystem. Read it and you know the current answer.
- asserted: AGENTS.md, docs/conventions/, docs/decisions/, docs/**/*.meiva.okf.md.
  Maintained and reviewed, but they can lag the code.
- remembered: the shared pool and your own memory. A snapshot, carrying an
  author and a timestamp.

Repo facts must carry their verification point — the path, command, or SHA
they were checked against; when you have not verified, mark the note
"unverified". A stale note's worst failure mode is looking plausible.

When two disagree, work out which one is current rather than following a fixed
ranking: check the self-verifying source when there is one, and weigh timestamps
and authorship when there is not. A memory the code contradicts is usually
stale — fix it at the source. A checked-in doc the code contradicts is worth
reporting, not silently working around."""


def memory_discipline_section(slices: AgentSlices, *, catalog: ModelCatalog) -> str:
    """Toggle via settings.agent.prompt_memory_behavior_enabled (env
    MEIVA_SYSTEM_PROMPT_MEMORY, default on). Empty when both stores are switched
    off — with nothing to write to, the discipline would describe a capability
    the agent does not have. The mechanics of reading/writing/searching live in
    the meiva.memory SDK docstrings and the two index notes, so this section
    deliberately does not repeat them."""
    from base.lm.registry import resolve_setting

    if not resolve_setting(
        "prompt_memory_behavior_enabled",
        model=slices.brain.llm_model,
        models=catalog.models,
        explicit=slices.read("agent", "prompt_memory_behavior_enabled"),
    ):
        return ""
    if not (
        slices.read("agent", "memory_index_inject_enabled")
        or slices.read("agent", "memory_per_agent_inject_enabled")
    ):
        return ""
    return _DISCIPLINE


# ── Passive memory recall hook ─────────────────────────────────────────


class _PassiveMemoryRecallHook(Hook):
    """Before an LLM turn, search the memory pool on the recent conversation and
    inject the notes that clear the score threshold as a note.

    No-op (returns None) when the feature is disabled, when the tail has no
    recallable inbound (only a watcher or shell wake-up), when auto-compact
    would replace messages this same turn
    (deferring avoids two before_llm hooks writing `messages` in one pass), or
    when recall finds nothing new to add. After a compaction it also replaces the
    window's record of injected notes, with or without a note: the compaction
    summarized them away, so they are neither excluded from the search nor counted
    against the gateway's `exclude` limit.

    The whole recall pass runs under `memory_recall_deadline_seconds`: recall
    is an enhancement sitting in front of the turn's first LLM call, so a
    congested gateway (a fleet wake queueing searches behind the search
    endpoint's semaphore) must degrade to "no recall this turn" instead of
    stalling the turn for the gateway's full queue time. The next turn retries.
    """

    async def __call__(
        self,
        state: AgentState,
        runtime: Runtime[MeivaContext],
        _config: RunnableConfig,
        /,
    ) -> dict[str, Any] | None:
        agent = runtime.context.require_agent()
        if not agent.memory.passive_memory_recall_enabled:
            return None

        version = state.compact.version
        stale = version > state.memory.window
        injected = set[str]() if stale else state.memory.injected_paths
        recall = await self._recall(state, runtime, injected=injected)
        if recall is not None:
            # Write only the fresh paths; the `memory` channel's reducer unions them onto
            # this window's record, or starts the record of a new window.
            return {
                "messages": [recall.note],
                "memory": MemoryState(injected_paths=recall.paths, window=version),
            }
        return {"memory": MemoryState(window=version)} if stale else None

    async def _recall(
        self,
        state: AgentState,
        runtime: Runtime[MeivaContext],
        *,
        injected: Collection[str],
    ) -> PassiveRecall | None:
        agent = runtime.context.require_agent()
        if not tail_has_recallable_inbound(state.messages):
            return None

        if auto_compact_will_fire(state, agent, catalog=runtime.context.require_catalog()):
            logger.info(
                "[{label}] {body}",
                label="passive-recall",
                body="defer: auto-compact predicted, skipping recall this turn",
                event="passive_recall",
            )
            return None

        deadline = agent.memory.memory_recall_deadline_seconds
        try:
            return await asyncio.wait_for(
                passive_memory_recall(
                    state.messages,
                    injected_paths=injected,
                    agent=agent,
                    context=runtime.context,
                ),
                timeout=deadline,
            )
        except TimeoutError:
            logger.info(
                "[{label}] {body}",
                label="passive-recall",
                body=(
                    f"skip: recall exceeded its {deadline:g}s deadline — "
                    "proceeding with no recall this turn"
                ),
                event="passive_recall",
            )
            return None


passive_memory_recall_before_llm = _PassiveMemoryRecallHook()


# ── Memory write check (after_exec hook) ────────────────────────────────


class _MemoryWriteCheckHook(Hook):
    """After an exec call that wrote memory notes with `meiva.files`, check them and add one
    reminder note when something needs the agent's attention.

    The wraps in `plugin.py` recorded the notes inside the exec subprocess; this hook reads the
    record, always clears it, and runs `check_written_notes` under
    `memory_write_check_deadline_seconds`. It never blocks, rewrites or rejects a write, and it
    never fails the turn: a check that runs out of time or hits an unexpected error is logged and
    dropped (the agent simply hears nothing about those notes). No-op, with no service call, when
    nothing was recorded.
    """

    async def __call__(
        self,
        state: AgentState,
        runtime: Runtime[MeivaContext],
        _config: RunnableConfig,
        /,
    ) -> dict[str, Any] | None:
        handle = state_handle()
        recorded = handle.view(state)
        if not recorded.shared_notes and not recorded.personal_notes:
            return None
        cleared = handle.delta(
            {"shared_notes": Overwrite(set()), "personal_notes": Overwrite(set())}
        )
        agent = runtime.context.require_agent()
        if agent.sandbox.eval_isolation:
            return cleared
        identity = runtime.context.identity
        agent_id = None if identity is None else identity.agent_id
        if agent_id is None:
            return cleared
        deadline = agent.memory.memory_write_check_deadline_seconds
        try:
            reports = await asyncio.wait_for(
                check_written_notes(
                    recorded.shared_notes,
                    recorded.personal_notes,
                    pool=memory_dir(),
                    personal_root=workspace_dir(agent_id) / "memory",
                    agent=agent,
                    context=runtime.context,
                ),
                timeout=deadline,
            )
        except TimeoutError:
            logger.info(
                "[memory-write-check] skip: the check exceeded its {:g}s deadline", deadline
            )
            return cleared
        except Exception:
            # A reminder must never cost the agent its turn: this hook runs inside the agent
            # process, where an escaping exception would end it.
            logger.opt(exception=True).error("[memory-write-check] check failed; no reminder")
            return cleared
        if not reports:
            return cleared
        note = system_note_message(
            content=render_reminder(reports),
            tag=NoteTag.MEMORY_WRITE_CHECK,
            created_at=datetime.now(UTC),
        )
        return {**cleared, "messages": [note]}


memory_write_check_after_exec = _MemoryWriteCheckHook()


def contribute() -> PluginContributions:
    """What this plugin declares for the agent runtime."""
    return PluginContributions(
        system_prompt_sections=(memory_discipline_section,),
        before_llm=(passive_memory_recall_before_llm,),
        after_exec=(memory_write_check_after_exec,),
        state=(MemoryWriteState,),
        context_notes=(
            ContextNote(memory_index_note, rank=RANK_CLUSTER_MEMORY),
            ContextNote(per_agent_memory_note, on_fork=True, rank=RANK_PER_AGENT_MEMORY),
            ContextNote(inherited_memory_note, on_fork=True, rank=RANK_INHERITED_MEMORY),
        ),
    )
