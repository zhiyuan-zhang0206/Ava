---
type: doc
title: meiva.memory — Long-term Memory Pool (Plugin Provided)
description: '`meiva.memory` is declared by the `meiva_memory` plugin as an `SdkNamespace`. Disabling the plugin → the entire `meiva.memory` becomes unavailable. Provides `PATH` (shared memory pool root directory), `PERSONAL_PATH` (your own memory folder) and `search()` (semantic search ranked by a decision model). Notes are written with `meiva.files`; a write check reminds the agent of format problems and likely duplicates.'
tags:
- plugin
- memory
- agent-view
- extensions
---

# meiva.memory — Long-term Memory Pool

## Attribution

**Provided by the `meiva_memory` plugin**. Not a core module — disabling the plugin makes `meiva.memory.PATH`, `meiva.memory.PERSONAL_PATH` and `meiva.memory.search()` entirely unavailable, and stops the write check.

## What it is

A shared markdown note folder (`~/.meiva/memory`), used by all agents. Notes are discovered via semantic search and synced across machines within approximately one day.

Two types of memory, each serving different audiences:
- **Shared pool** (`meiva.memory.PATH`): notes visible to all agents. Write durable facts that other agents need when taking over your role — user preferences, hard constraints, long-term decisions.
- **Per-agent memory** (`meiva.memory.PERSONAL_PATH`, `<workspace>/memory/`): your own durable state, maintained by yourself (the agent keeps its `MEMORY.md` index by hand), surviving across compactions.

### Inheritable blocks (personal store → descendants)

A personal entry may fence content descendants should receive:

```markdown
<!-- meiva:inheritable -->
Standing guidance descendants should receive.
<!-- /meiva:inheritable -->
```

- Read at every context-window establishment (cold start / post-compact / fork regraft) from the first `memory_inherit_depth` (default 1, 0=off, ≤10) hops up the birth chain, nearest ancestor first; multiple fences and multiple entries concatenate in order.
- Only ancestors that ran on the same machine contribute; a remote ancestor is skipped and named in the note's footer.
- Size guardrails: `memory_inherit_max_block_chars` (4000) / `memory_inherit_max_total_chars` (16000) — over either, the note carries a visible `[truncated …]` marker and the log a warning; 0 disables one. Malformed fences (unclosed / stray close) drop the block with a warning — never a silent over-share.
- Content is a pure function of (chain, files, settings) — no timestamps — so unchanged state re-renders byte-identical (fork prefix-cache stability).

## Core API

- `PATH: PosixPath` — shared memory pool root directory `~/.meiva/memory`; its `help()` text is the note format an agent follows
- `PERSONAL_PATH: PosixPath` — the agent's own memory folder, `<workspace>/memory`, resolved from the current SDK identity (an attached external lease first, then the process identity) when read; raises when no identity is bound
- `search(query, k=5, *, timeout=None) → list[MemoryMatch]` — semantic search; returns up to `k` `MemoryMatch(path, description, tags, score, chunk)` named tuples, most relevant first. `path` is absolute, `score` is a decision model's probability (0..1) that the matched `chunk` (the note's best passage, or its description line) is relevant to the query, and every match is returned whatever its score — the caller decides what a weak match deserves. `timeout` bounds one attempt (default = the gateway's own search deadline + 3s, 18s); when given it also travels in the request as the search's deadline, so the gateway stops ranking when it passes, and the HTTP timeout is `timeout` + 3s. A congested index answers 503 (`IndexerUnavailable`) in ~1s instead of queueing the call. A search returns at most the notes one search ranks (`memory_rerank_candidates`, 30), whatever `k` is
- `MemoryMatch` — the result tuple above, exported in `__all_for_meiva__` so `help(meiva.memory)` renders its fields
- `IndexerUnavailable` — exception when the indexer service is unavailable, or when the decision model that ranks results fails (search never falls back to unranked vector order)

Search is two stages, both on the gateway: vector search returns the best chunk of the
closest `memory_rerank_candidates` notes (default 30), then the decision model
`memory_rerank_model` answers a yes/no "worth reading for this query" question for each chunk (one request per candidate; `purpose` tells recall from an agent search) and the response is
ordered by the probability of yes. That count is the most decision requests one search issues: neither `k` nor the request's `exclude` list (paths dropped before ranking, used by passive recall; internal, not an `meiva.memory.search` parameter) changes it. No text is generated. The gateway response contains only
`results`, each with a memory-root-relative `path`, frontmatter `description` and `tags`, the
`score` and the matched `chunk`. The SDK resolves each path against the runner's memory root;
the HTTP response has no duplicate bare-path list.

Personal-index injection and `PERSONAL_PATH` resolve the current SDK identity:
an attached external lease takes precedence over a hosted turn, then the process
identity. External lease validity is checked before reading or creating memory
files. A note written with `meiva.files` takes effect on the filesystem immediately;
it is not a plugin state journal entry and does not wait for the impersonation handoff.

## Writing a note

There is no writer function: the agent writes the whole note, frontmatter included, with `meiva.files.write` / `append` under `PATH` or `PERSONAL_PATH`, following the format in `help(meiva.memory.PATH)`. `meiva.files` stores the content as given. What happens afterwards (rule and duplicate reminders) is [[meiva_builtins/plugins/meiva_memory/docs/memory-write-check.meiva.okf.md]].

## Key Dependencies

- [[meiva_builtins/plugins/meiva_memory/docs/meiva_memory.meiva.okf.md]] — the owning plugin
- [[services/docs/gateway_side/memory_indexer/memory-indexer.meiva.okf.md]] — vector index service
- [[services/docs/gateway_side/memory_search/memory-search.meiva.okf.md]] — the vector store service
- [[meiva_builtins/plugins/meiva_memory/docs/memory-recall.meiva.okf.md]] — passive recall mechanism (provided by the same plugin)
- [[meiva_builtins/plugins/meiva_memory/docs/memory-write-check.meiva.okf.md]] — the reminders on notes written with `meiva.files`
