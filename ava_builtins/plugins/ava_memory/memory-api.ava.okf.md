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

## Core API

- `PATH: PosixPath` — shared memory pool root directory `~/.ava/memory`
- `search(query, k=5) → list[tuple[Path, str, list[str]]]` — semantic search, returns `(absolute path, frontmatter description, tags)` tuples for the most relevant notes
- `write(slug, content, *, title=None, description=None, tags=None, store="personal") → Path` — writes an absolute personal or shared entry with generated frontmatter and upserts its `MEMORY.md` pointer; the canonical writer, immune to `ava.cwd` drift
- `IndexerUnavailable` — exception when the indexer service is unavailable

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
