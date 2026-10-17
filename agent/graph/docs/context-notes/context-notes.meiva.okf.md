---
type: doc
title: Standing Context — What Sits Behind the System Prompt
description: The standing head of an agent's context window — the SystemMessage plus the ordered context notes behind it, who owns them, and how memory notes reach them. Laid down by the `init_context` node whenever a window is established.
tags: []
---

# Standing Context — What Sits Behind the System Prompt

## What It Is

Standing context consists of the system prompt and ordered context notes.
`init_context` establishes it on first wake and after compaction.

Notes tell the agent what changed and what to do, in its own terms.

## Core Mechanisms

### Standing Context Notes (`context_notes.py`, laid down by the `init_context` node)
- One ordered list, one owner: `init_context` runs before `claim` and lays down the head (SystemMessage + notes) whenever `messages` is empty. A compaction empties the window, parks the tail in `state.context_reset`, and routes back here, so cold start and post-compact are one code path.
- Framework notes (`FRAMEWORK_NOTES`): agent id (`on_fork`; states id, label, machine, workspace path), exec timeout, cluster timezone, preloaded skills (`on_fork`, issue #1320). A plugin declares its own in `contribute()`; `MeivaContext.extensions` supplies them ([[docs/plugins/declared-contributions.meiva.okf.md]]). `on_fork` = grafted onto a fork's inherited history (`fork_notes`).
- Rank is a prompt-cache decision as much as a reading-order one. Ranks 10-19 are the stable band: cluster-identical bytes (one cached prefix shared by every agent on the API key) that change only on a config edit already requiring an agent restart. Rank 20 — the shared memory index — is re-read off disk at every establishment, so anything behind it re-caches whenever any agent writes memory. The cluster-timezone note sits at rank 15 for exactly this reason.
- **Cluster timezone** (rank 15): states `settings.general.timezone` by IANA name plus its current numeric offset. It exists so the timestamps do not have to carry one — the setting is cluster-pinned, so the suffix was a constant repeated on every stamp, and an ambiguous constant at that (`%Z` is `PDT` in June and `PST` in December for one unchanged setting; `CST` names two zones). Not `on_fork`: a fork stays in the same cluster, so the inherited declaration is still true.
- **Agent identity** (rank 30): states id, label, host machine, and workspace. Missing rows or empty labels omit the label clause; SQL and programming errors propagate to the context-building caller. An unset machine name omits its clause. The `# Workspace` prompt section is id-free for fork safety: this `on_fork` note holds the concrete path, and a fork regrafts its own. Each builder receives `MeivaContext`: identity uses `agent_id(ctx)` with lease validation, labels use `ctx.sql`, and settings use `ctx.agent`; there is no host-wide SDK binding.
- **Fork strip + graft (issue #1320)**: `_handle_fork` rebuilds the head — a full wipe (`RemoveMessage(__remove_all__)`, the one deletion shape the append-only ruling sanctions) plus the inherited history re-listed with the source-identity notes (`agent_id` / `agent_memory` / `inherited_memory` / `preloaded_skills`, each names the source) dropped — then grafts `fork_notes()`: the `on_fork` subset rebuilt with the new agent's content. The shared memory index is NOT `on_fork`: cluster-wide, the inherited copy is what a graft would add (grafting it duplicated the index — the timezone rule). Everything in front of the first dropped note survives byte-identical — that prefix is the provider cache the fork rides (docs/decisions/runtime/config/2026-09-10-preset-in-config-overlay-fork-cache.md).
- **Fork tail-graft skills (task #2694)**: the fork inbound's payload carries `tail_skills` — inject-list skills the fork's config added beyond the source's, minus what the fork's expand list already grafts. `_handle_fork` resolves them (the shared `resolve_prompt_skills`, warn-and-skip) and appends one note with their full SKILL.md bodies AFTER the fork marker and `fork_notes()` — the tail, never the cached prefix. Tagged `preloaded_skills` so the next fork strips it with the other skill notes. Expand-list additions need no extra mechanism: the fork's own preloaded-skills note already covers them.

### Memory Injection (`plugins/meiva_memory/notes.py` + `inherit.py`, declared by the plugin)
- Both stores belong to `meiva_memory`; disabling it removes stores, SDK surface, notes, and discipline section together. The three notes — shared pool index, per-agent index, and the `inheritable` blocks read from the ancestor chain — their ranks, switches, fork behavior, and guardrails are documented in [[agent/graph/docs/context-notes/memory-injection.meiva.okf.md|Memory Injection]].

### Passive Recall (`memory_recall.py`)
- Content-driven recall on **new inbound wake-up turns** (not agent-initiated search). The trigger gate skips machine-originated wake-ups (`watcher:` / `shell:` prefixes) and fires on user chat, peer-agent (`agent:`), scheduled (`schedule:`), and system inbound. **One search, scored by a decision model**: the gateway retrieves the best chunk of the closest `memory_rerank_candidates` (30) notes and a decision model (`memory_rerank_model`) scores each chunk's relevance to the conversation; recall injects the notes scoring at least `memory_recall_inject_threshold` (0.80), best first, at most `memory_recall_inject_max` (3), each with the passage that matched. Vector search always returns its top-k, so the score threshold is what keeps weak matches out of context; a turn where nothing reaches it injects nothing. Recall makes no model call of its own.
- Never fatal: a failed search (index or ranking model unavailable, or the gateway answering with an error status) → no-op, logged, and the next turn retries; the hook runs in `before_llm`, so an exception escaping it would end the agent process, not just the recall. There is no fallback to unscored vector order. Same-window dedup happens after the threshold and before the cap, so already-injected notes never use up an injection slot.
- `settings.agent.passive_memory_recall_enabled` gates the stage. Its default, the threshold and the cluster opt-out are stated in [[meiva_builtins/plugins/meiva_memory/docs/memory-recall.meiva.okf.md]], not here.

### Context Structure
- System prompt (fixed, included in every LLM call)
- Standing context notes (exec timeout / cluster timezone / agent id / preloaded skills / the two memory indexes / the chain-inherited memory blocks) laid down behind the system prompt whenever the window is established
- Message history (user messages + agent replies + tool call results)
- Current turn input


## Key Dependencies

- [[context-window.meiva.okf.md]] — compaction is what makes the head need re-establishing
- [[system-prompt.meiva.okf.md]] — the head's first message, and where the memory discipline lives as a section
- [[meiva_builtins/plugins/meiva_memory/docs/memory-recall.meiva.okf.md]] — passive recall in depth

## Entry Points
- `agent/graph/_init_context.py` — the node that lays down the head
- `agent/graph/prompt/context_notes.py` — `context_notes` / `fork_notes`, framework notes
- `meiva_builtins/plugins/meiva_memory/notes.py` — the two memory index notes; `inherit.py` — the inherited-memory note (ancestor chain, block parsing, guardrails)
- `agent/graph/recall/memory_recall.py` — passive recall
