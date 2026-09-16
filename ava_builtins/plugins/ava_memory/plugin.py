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

# This module is the plugin's SDK **surface** — the only face an agent-launched
# child loads (task #3633). Its agent-runtime registrations (the two context-note
# indexes, the memory-discipline prompt section, the passive-recall before_llm
# hook) live in `agent_runtime.py`, imported only on the full path (see
# `agent/_extensions.py`).


import ava as _ava

from . import sdk as _memory_sdk

# ── ava.memory namespace registration ──────────────────────────────────
# The SDK surface (PATH / search / write) lives in `sdk.py` — a real module
# that IS the `ava.memory` namespace. register_namespace puts the same object
# on the package (attribute access) and in sys.modules under `ava.memory`
# (`import ava.memory`), so both spellings resolve identically.
_ava.register_namespace("memory", _memory_sdk)
