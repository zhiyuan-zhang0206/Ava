---
type: doc
title: ava_memory — Memory Pool Plugin
description: '`ava_memory` provides shared memory pool capability: passive memory recall (auto semantic search on new user message arrival and inject relevant note references) and the `ava.memory.*` SDK surface.'
tags:
- extensions
- plugin
- agent-extension
---

# ava_memory — Memory Pool Plugin

## What it is

`ava_memory` provides shared memory pool capability: passive memory recall (`AVA_PASSIVE_MEMORY_RECALL`) and the `ava.memory.*` SDK surface. Whether recall runs, and the knobs that shape it, are [[ava_builtins/plugins/ava_memory/memory-recall.ava.okf.md]]'s to state.

## Registered hooks

### Passive memory recall (before_llm hook)

```python
class _PassiveMemoryRecallHook(Hook):
    async def __call__(
        self, state: AgentState, _runtime: Runtime[AvaContext], _config: RunnableConfig, /
    ) -> dict | None: ...

passive_memory_recall_before_llm = _PassiveMemoryRecallHook()
register_before_llm(passive_memory_recall_before_llm)
```

**Trigger conditions**:
- **Feature gate**: `settings.passive_memory_recall_enabled` (env `AVA_PASSIVE_MEMORY_RECALL`) — the entire hook is a no-op when it is off; its default and the cluster opt-out are in [[ava_builtins/plugins/ava_memory/memory-recall.ava.okf.md]]
- Positive gate `tail_has_user_inbound` (`agent/messages.py:223-232`): **since last AIMessage** any inbound whose source **does not start with `agent:`** triggers — user / system / schedule / watcher / shell sources **all pass**, **only peer agent (`agent:` prefix) is excluded**; no non-empty text check, not "most recent one", but scans tail inbound
- If auto-compact hook would also trigger in the same turn, **defers** (returns None) — `messages` have add_messages reducer that merges, without failing loud, but auto-compact's REMOVE_ALL full replacement is order-sensitive and would swallow the same-turn appended note

**Behavior**:
- Performs semantic search against recent conversation, injects matched note references as system messages (`system_note`), seen by agent in next turn. Each hit presents the same fields as `ava.memory.search` — path + frontmatter description (empty string when absent, not synthesized from title/body)
- Uses the built-in `memory` (`MemoryState`) sub-state's `injected_paths` union reducer (`_memory_state_merge`) to accumulate path set, each note appears at most once

The recall engine (search + rendering beyond gating) is in the core `agent/graph/_memory_recall.py:passive_memory_recall`;
this plugin only provides the before_llm wiring + trigger gate. It also bundles memory skills: `ava.skills.ava_memory`
(Memory Steward maintenance manual — daily merge, health check, note consolidation, query service) +
`ava.skills.ava_memory.consolidation` sub-skill (`ava memory` CLI daily merge — commit / push / rebuild index).

**Host-side scaffolding** (`setup.py:scaffold()`, run by the converge phase for every enabled plugin): brings up this cluster's memory pool checkouts and lays the plugin's `template/` down inside the authoring one — `MEMORY.md` (the index every agent reads) and `.githooks/pre-commit` (the character-cap guard), then arms `core.hooksPath`. Idempotent and non-destructive; never overwrites a file the pool already has. Kept out of `plugin.py` because that module imports the agent runtime, which does not exist in the `ava` CLI process.

**`python3 validate.py`** (in the pool checkout, run by the pre-commit hook): the pool's rules in one place — frontmatter, exactly one `type/<x>` tag, a non-empty `description`, the index's pointer format (`- [Title](path.md) — description`) and targets, orphaned notes, the character caps, and the directory-structure limits. Non-zero exit on any finding; the same check runs automatically on every commit via `.githooks/pre-commit`.
Skill nodes see [[ava_builtins/plugins/ava_memory/skills/skills.ava.okf.md|Ava Memory Skills]].

## Provided SDK surface

This plugin **owns** the `ava.memory` namespace — `plugin.py` assembles it and calls `register_namespace("memory", ...)`. There is no core `ava/memory.py`; disabling the plugin removes the surface entirely.

- `ava.memory.PATH` — memory pool root path (`$AVA_HOME/memory`, computed by `ava_home()`), shared by all agents
- `ava.memory.search(query, k=5)` — semantic search, returns `list[(path, description)]`
  (description from note YAML frontmatter, empty string when absent)
- `ava.memory.search_detailed(query, k=5)` — the same one search, returning `list[(path, description, tags)]`
  so a caller can weigh a hit by its `type/<x>` tag rather than only see which hits there are

Writing memory notes directly uses `ava.files.write` to write markdown into `ava.memory.PATH` — **there is no `ava.memory.write`** (`__all_for_ava__` is `PATH`, `search`, `search_detailed`; `IndexerUnavailable` is reachable but deliberately excluded from `help(ava.memory)`).

## Key dependencies

- [[agent/hooks/hooks.ava.okf.md]] — before_llm hook system
- [[agent/graph/context-notes.ava.okf.md]] — where this plugin's index notes are laid down
- [[context-window.ava.okf.md]] — memory injection affects context
- [[memory-indexer.ava.okf.md]] — background indexing service
- [[shared/lm/lm.ava.okf.md]] — semantic search uses LLM embeddings

## Configuration

- Memory pool path = `$AVA_HOME/memory` (computed by `ava/memory.py` at process load using `ava_home()`; no `AVA_MEMORY_POOL` variable)
- `settings.passive_memory_recall_enabled`: passive recall feature toggle (default in [[ava_builtins/plugins/ava_memory/memory-recall.ava.okf.md]])
- Memory note format: YAML frontmatter (`type: Memory`, `ava_agent`, etc.) + markdown body

## Notes

- Memory notes are shared cross-agent — a note written by one agent can be searched by another
- Passive recall is "on-demand" rather than "full" — only triggers when tail has non-agent inbound, avoiding search on every turn
- The mutual exclusion with auto-compact is carefully designed — `messages` have a reducer that merges, but compact's REMOVE_ALL full replacement swallows same-turn appended notes, memory recall yields priority to compaction
