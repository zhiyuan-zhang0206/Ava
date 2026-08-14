---
description: Wind down and compact your context — housekeeping first, then a self-authored summary
instruction-hint: optional focus, e.g. "keep the latest review thread verbatim"
---

Wind down your working context and compact it now.

First, persist anything durable to where it belongs — workspace notes, handoff docs, PR descriptions, and the runtime state you are tracking (sub-agent handles, what you delegated, open todos) — so the summary only has to point back to them, not reproduce them. If you are waiting on something, set a watcher.

Then call ava.self.compact(summary), written exactly as its docstring specifies — that contract is the single source of truth for what a good summary contains and how to write it.
