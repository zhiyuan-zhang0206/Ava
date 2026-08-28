"""ava_memory plugin — shared memory pool: the full `ava.memory` surface + passive recall hook + daily consolidation skill.

Three capabilities live under one plugin because they share the same domain (the
shared memory pool at ava.memory.PATH):

1. **ava.memory namespace** (PATH + search + write) — the agent-facing SDK surface.
   This plugin OWNS the `ava.memory` top-level namespace. Disabling it removes
   `ava.memory` entirely — PATH, search(), write(), IndexerUnavailable — not just
   passive recall.

2. **Passive memory recall** (before_llm hook) — on a turn woken by fresh user
   inbound, searches the pool semantically on the recent conversation and
   injects the top matching note references so relevant durable notes surface
   without the agent asking. The recall engine lives in
   agent/graph/_memory_recall.py; this plugin is the wiring plus its firing
   gates.

3. **Memory stewardship + daily consolidation** (bundled skills) — the pool is
   a git repo. Once a day the day's notes are committed, pushed, and re-indexed
   so search stays current. The procedure varies by deployment: single-box runs
   `skills/scripts/consolidate.py`; multi-host spawns one steward per machine, then
   the arbiter merges all PRs. The stewardship playbook (role, health checks,
   note merging, query service) is at ava.skills.ava_memory; the CLI-backed
   consolidation procedure is the ava.skills.ava_memory.consolidation
   sub-skill.

Disabling this plugin removes the entire ava.memory surface, passive recall,
and the bundled memory skills.

Passive recall in detail:

Where the standing memory index keeps MEMORY.md permanently in front of the
agent, passive recall reaches into the *rest* of the pool: on a turn woken by
fresh inbound, it runs a semantic search keyed on the recent conversation
and injects the top matches (path + frontmatter description, the same fields
ava.memory.search returns) as a system-styled note. The agent sees durable
notes relevant to what is being said without having to call ava.memory.search
itself.

The heavy lifting (query build, search, dedup vs already-injected paths, note
rendering) lives in agent/graph/_memory_recall.py; this plugin is just the
before_llm wiring plus its firing gates.

Firing gates:
- Feature gate: no-op unless `turn_settings.agent.passive_memory_recall_enabled`.
- Trigger gate: fire when the message tail carries fresh inbound from a real
  source — user chat, a peer agent (`agent:`), a scheduled turn (`schedule:`),
  or a system notice — and skip machine-originated wake-ups (`watcher:` /
  `shell:` prefixes; `tail_has_recallable_inbound`). A silent-idle continue or a
  lifecycle marker carries no inbound at all and is skipped too. This same tail
  shape (an inbound sits after the last AIMessage) makes recall mutually
  exclusive with hooks that fire on a bare AIMessage tail (e.g. silent-idle), so
  they never contend for the write.

Same-session dedup: the injected paths accumulate in `state.memory` via the
`memory` channel's union reducer (`_memory_state_merge`); this hook passes the
accumulated set into the recall call and writes back only the fresh paths, so a
note surfaces at most once per session.

compact clobber-safety: auto-compact is also a before_llm hook. `messages`
carries the add_messages reducer, so co-writing it does NOT fail-loud — the
runner merges both hooks' values. But auto-compact's full-history REMOVE_ALL
replacement is order-sensitive and would swallow a note appended in the same
pass. So when auto-compact would fire this same turn, this hook defers
(returns None) rather than racing the history replacement; recall simply
retries on the next turn.
"""

from __future__ import annotations

__description__ = "Shared memory pool: ava.memory SDK surface (PATH + search + write) + passive recall hook (auto-surfaces relevant notes) + daily consolidation skill (commit, push, re-index)"


from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

import ava as _ava
from agent.graph._context import AvaContext
from agent.graph._context_notes import (
    RANK_CLUSTER_MEMORY,
    RANK_PER_AGENT_MEMORY,
    register_context_note,
)
from agent.graph._memory_recall import passive_memory_recall
from agent.graph._system_prompt import register_system_prompt_section
from agent.hooks import Hook, register_before_llm
from agent.hooks.compact import auto_compact_will_fire
from agent.messages import tail_has_recallable_inbound
from agent.state import AgentState, MemoryState
from shared.config import settings
from shared.config.turn_view import turn_settings
from shared.log import logger

from . import sdk as _memory_sdk
from .notes import memory_index_note, per_agent_memory_note

# The two memory indexes join the framework's ordered context-note registry, so
# `init_context` lays them down whenever a window is established. The ranks pin
# them into the reading order the framework documents (see the rank scale in
# `_context_notes.py`): the shared index right after the exec-timeout note, the
# per-agent index right after the agent-id note. Registered here rather than by
# a decorator in notes.py: this module is re-executed on every plugin (re)load,
# while importing notes.py hits the sys.modules cache — so decorators there
# would not survive a clear_plugin_registrations cycle. Disabling this plugin
# means neither is ever registered, and a window is laid down with no memory
# notes in it — matching an agent that has no memory stores.
# The shared index is NOT `on_fork`: the pool is cluster-wide, so the copy
# a fork inherits with the source's history is the same content the graft would
# add — grafting it duplicates the index in the forked agent's window
# (issue #1320). Same reasoning as the framework's timezone note. The per-agent
# index IS `on_fork`: it names the source agent's store, which the inherited
# history renders wrong for the new agent — `_handle_fork` strips the inherited
# copy before grafting the new agent's own.
register_context_note(rank=RANK_CLUSTER_MEMORY)(memory_index_note)
register_context_note(on_fork=True, rank=RANK_PER_AGENT_MEMORY)(per_agent_memory_note)


# ── Memory discipline (system prompt section) ──────────────────────────

# The write discipline both stores share. It is fixed text — the same bytes for
# every agent on every turn — so it belongs in the system prompt, not in a note
# beside the indexes: an index carries content that changes as the agent writes,
# this does not.
_DISCIPLINE = """\
# Remembering across sessions

Your memory — two stores, one discipline.

- Shared pool (ava.memory.PATH): notes every agent can read. Restrained by
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
as finished. An offered next step is a finished engagement, not permission to
defer.

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

- self-verifying: running code, ava cluster status, the database, the
  filesystem. Read it and you know the current answer.
- asserted: AGENTS.md, conventions/, decisions/, okf/*.ava.okf.md.
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


@register_system_prompt_section
def memory_discipline_section() -> str:
    """Toggle via settings.agent.prompt_memory_behavior_enabled (env
    AVA_SYSTEM_PROMPT_MEMORY, default on). Empty when both stores are switched
    off — with nothing to write to, the discipline would describe a capability
    the agent does not have. The mechanics of reading/writing/searching live in
    the ava.memory SDK docstrings and the two index notes, so this section
    deliberately does not repeat them."""
    from shared.lm.registry import resolve_setting

    if not resolve_setting("prompt_memory_behavior_enabled", model=turn_settings.lm.llm_model):
        return ""
    if not (
        settings.agent.memory_index_inject_enabled or settings.agent.memory_per_agent_inject_enabled
    ):
        return ""
    return _DISCIPLINE


# ── Passive memory recall hook ─────────────────────────────────────────


class _PassiveMemoryRecallHook(Hook):
    """Before an LLM turn, search the memory pool on the recent conversation and
    inject fresh top matches as a note.

    No-op (returns None) when the feature is disabled, when the tail has no
    recallable inbound (only a watcher or shell wake-up), when auto-compact
    would replace messages this same turn
    (deferring avoids two before_llm hooks writing `messages` in one pass), or
    when recall finds nothing new to add.
    """

    async def __call__(
        self,
        state: AgentState,
        _runtime: Runtime[AvaContext],
        _config: RunnableConfig,
        /,
    ) -> dict | None:
        if not turn_settings.agent.passive_memory_recall_enabled:
            return None

        if not tail_has_recallable_inbound(state.messages):
            return None

        if auto_compact_will_fire(state):
            logger.info(
                "[{label}] {body}",
                label="passive-recall",
                body="defer: auto-compact predicted, skipping recall this turn",
                event="passive_recall",
            )
            return None

        recall = await passive_memory_recall(
            state.messages, injected_paths=state.memory.injected_paths
        )
        if recall is None:
            return None

        # Write only the fresh paths; the `memory` channel's union reducer
        # accumulates them onto the current set.
        return {
            "messages": [recall.note],
            "memory": MemoryState(injected_paths=recall.paths),
        }


passive_memory_recall_before_llm = _PassiveMemoryRecallHook()
register_before_llm(passive_memory_recall_before_llm)

# ── ava.memory namespace registration ──────────────────────────────────
# The SDK surface (PATH / search / write) lives in `sdk.py` — a real module
# that IS the `ava.memory` namespace. register_namespace puts the same object
# on the package (attribute access) and in sys.modules under `ava.memory`
# (`import ava.memory`), so both spellings resolve identically.
_ava.register_namespace("memory", _memory_sdk)
