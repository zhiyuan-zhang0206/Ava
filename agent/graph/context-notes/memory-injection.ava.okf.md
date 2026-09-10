---
type: doc
title: Memory Injection — The Three Memory Notes Behind the System Prompt
description: How ava_memory's three standing context notes reach a window — the shared pool index, the per-agent index, and the inheritable blocks inherited from the ancestor chain — plus their injection moment, switches, and fork behavior.
tags:
- memory
- context
---

# Memory Injection — The Three Memory Notes

## Responsibility

`ava_memory` owns every memory context note; disabling the plugin removes the
stores, the SDK surface, the notes, and the write-discipline section together
(`notes.py` builds the two index notes, `inherit.py` the inherited one,
`plugin.py` registers all three). All are laid down by `init_context` behind
the SystemMessage; see [[agent/graph/context-notes/context-notes.ava.okf.md|Standing
Context]] for the registry itself.

## The notes

- **Shared MEMORY.md** (rank 20; memory pool root `ava.memory.PATH`, pointer
  index, visible to all agents) — `settings.agent.memory_index_inject_enabled`.
  Not `on_fork` (cluster-wide; grafting duplicated it — issue #1320).
- **Per-agent memory** (rank 40; workspace `memory/` dir: `memory/MEMORY.md`
  index, entries as sibling files read on demand) —
  `settings.agent.memory_per_agent_inject_enabled`, `on_fork`. Empty index
  created if missing; a legacy single-file `<workspace>/MEMORY.md` migrates in
  on first injection, never overwriting. **Only the index is injected**; past
  `memory_per_agent_index_max_lines` (default 200, 0=off) a maintenance
  reminder is appended — no truncation.
- **Inherited memory** (rank 45; `inherit.py`): the `inheritable` blocks an
  agent's ancestors fenced inside their personal entries
  (`<!-- ava:inheritable -->` … `<!-- /ava:inheritable -->`), read at every
  window establishment from the first `memory_inherit_depth` hops (default 1,
  0..10, 0=off) up the immutable birth chain (`agents_meta.born_spawner`;
  on a fork the chain is the fork source's). Chain read: one light gateway call
  (`GET /api/agents/{id}/born-chain`, no tie graph), cached per process — the
  chain is immutable; a failed read degrades to no note and retries at the next
  establishment. Content is read off THIS machine's workspaces, so only
  ancestors that ran here contribute — a remote ancestor is skipped and named
  in a footer (same-machine read, v1). Size guardrails
  (`memory_inherit_max_block_chars` / `memory_inherit_max_total_chars`, 0=off)
  truncate oversized declarations with a visible marker plus a warning — never
  silently. Content carries no timestamps, so unchanged state is byte-stable.
  `on_fork`: the inherited note carries the SOURCE's chain, so `_handle_fork`
  strips it and regrafts the new agent's own.

Malformed fences (unclosed / stray close / nested open) drop the block with a
warning; only a file's body counts, split by the writer's own frontmatter rule
(`sdk._frontmatter_parts`), so a marker inside frontmatter never opens a block.

## Write discipline

The discipline both stores share (criteria, mandatory-write triggers, the
`type/*` vocabulary, weighing a memory against self-verifying vs asserted
sources) is a **system prompt section** owned by the same plugin — fixed text,
where an index is not.
