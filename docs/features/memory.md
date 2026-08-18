# Memory

Ava agents share a memory pool that outlives any conversation: markdown notes
plus semantic search, visible to every agent.

## Why it matters

- **Long-lived agents** — tasks are handed off through notes, not conversation
  context; restart, compaction, or a new agent taking over loses nothing.
- **Shared knowledge** — a pitfall one agent hits, a rule the user set, the
  current state of a project: write a note and the whole fleet can find it.
- **Self-maintaining** — a steward agent consolidates, health-checks, and
  commits the pool daily; it does not rot.

## How it works

```
agent writes note → shared pool (markdown + frontmatter)
agent asks → semantic search (ava.memory.search)
cold start / after compact → memory index injected into context
```

The index (`MEMORY.md`) is injected at cold start and after every compaction,
so standing rules stay in front of the agent instead of decaying out of
context. The pool is a git repo of its own, consolidated daily by the steward.

## Design decisions

- [Memory steward role](../../decisions/2026-07-11-memory-steward.md)
- [Memory arbiter role and schedule](../../decisions/2026-08-01-memory-arbiter-role-and-schedule.md)
