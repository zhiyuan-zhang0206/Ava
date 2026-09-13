---
type: doc
title: ava.memory — Long-term Memory Pool (Plugin Provided)
description: '`ava.memory` is registered by the `ava_memory` plugin via `register_namespace`. Disabling the plugin → the entire `ava.memory` becomes unavailable. Provides `PATH` (shared memory pool root directory), `search()` (semantic search), and `write()` (cwd-independent indexed memory authoring).'
tags:
- plugin
- memory
- agent-view
- extensions
---

# ava.memory — Long-term Memory Pool

## Attribution

**Provided by the `ava_memory` plugin**. Not a core module — disabling the plugin makes `ava.memory.PATH`, `ava.memory.search()`, and `ava.memory.write()` entirely unavailable.

## What it is

A shared markdown note folder (`~/.ava/memory`), used by all agents. Notes are discovered via semantic search and synced across machines within approximately one day.

Two types of memory, each serving different audiences:
- **Shared pool** (`ava.memory.PATH`): notes visible to all agents. Write durable facts that other agents need when taking over your role — user preferences, hard constraints, long-term decisions.
- **Per-agent memory** (`<workspace>/memory/`): your own durable state, maintained by yourself, surviving across compactions.

### Inheritable blocks (personal store → descendants)

A personal entry may fence content descendants should receive:

```markdown
<!-- ava:inheritable -->
Standing guidance descendants should receive.
<!-- /ava:inheritable -->
```

- Read at every context-window establishment (cold start / post-compact / fork regraft) from the first `memory_inherit_depth` (default 1, 0=off, ≤10) hops up the birth chain, nearest ancestor first; multiple fences and multiple entries concatenate in order.
- Only ancestors that ran on the same machine contribute; a remote ancestor is skipped and named in the note's footer.
- Size guardrails: `memory_inherit_max_block_chars` (4000) / `memory_inherit_max_total_chars` (16000) — over either, the note carries a visible `[truncated …]` marker and the log a warning; 0 disables one. Malformed fences (unclosed / stray close) drop the block with a warning — never a silent over-share.
- Content is a pure function of (chain, files, settings) — no timestamps — so unchanged state re-renders byte-identical (fork prefix-cache stability).

## Core API

- `PATH: PosixPath` — shared memory pool root directory `~/.ava/memory`
- `search(query, k=5, *, timeout=None) → list[tuple[Path, str, list[str]]]` — semantic search, returns `(absolute path, frontmatter description, tags)` tuples for the most relevant notes; `timeout` bounds one attempt (default = the gateway's own search deadline + 3s, 18s), and a congested index answers 503 (`IndexerUnavailable`) in ~1s instead of queueing the call
- `write(slug, content, *, title=None, description=None, tags=None, store="personal") → Path` — writes an absolute personal or shared entry, assembling the note's frontmatter (a block the content carries stays the note's only one, is completed with the missing fields, and has its bare values quoted where YAML would misread them) and upserting its `MEMORY.md` pointer (shared topic-directory entries are exempt: their line lives in the directory's own `index.md`); the canonical writer, immune to `ava.cwd` drift
- `IndexerUnavailable` — exception when the indexer service is unavailable

Memory authoring and personal-index injection resolve the current SDK identity:
an attached external lease takes precedence over a hosted turn, then the process
identity. External lease validity is checked before reading or creating memory
files. Writes take effect on the filesystem immediately; they are not plugin
state journal entries and do not wait for the impersonation handoff.

## Note Format

```markdown
---
type: Memory
ava_agent: <your id>
---
<!-- agent-<your id> @ <your machine>, YYYY-MM-DD HH:MM -->
```

## Key Dependencies

- [[ava_builtins/plugins/ava_memory/ava_memory.ava.okf.md]] — the owning plugin
- [[services/gateway_side/memory_indexer/memory-indexer.ava.okf.md]] — vector index service
- [[services/gateway_side/milvus.ava.okf.md]] — vector database
- [[ava_builtins/plugins/ava_memory/memory-recall.ava.okf.md]] — passive recall mechanism (provided by the same plugin)
