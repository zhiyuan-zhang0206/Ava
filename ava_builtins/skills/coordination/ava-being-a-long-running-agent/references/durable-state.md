# Durable state and recovery records

Read this reference only for the task or mode selected by the skill entrypoint.
Run command examples from the original skill directory unless they specify
another working directory.

## Two kinds of state, three destinations
Your state splits across three stores with different audiences:

| Store | Audience | What goes there |
|-------|----------|-----------------|
| **Workspace** (`ava.cwd`) | You (on demand) | Task files, drafts, logs, artifacts. Detailed working files you read when needed. |
| **Your memory** (`<workspace>/memory/`) | You (index always injected) | Your durable state: role, preferences, ongoing responsibilities, known pitfalls. `memory/MEMORY.md` is the index — injected into every context; each memory is one file beside it, read on demand. |
| **Shared memory** (`ava.memory`) | Every agent | Facts another agent would need to take over your role. Shared, searchable. |

### Your memory vs compact summary

| | Compact summary | Your memory |
|---|---|---|
| **What** | What happened in one conversation round | Who you are as an agent |
| **When** | Replaced at each compaction | Persists across compactions |
| **Contains** | Requests, progress, dead ends, verbatim tail | Role, preferences, responsibilities, pitfalls |

Each compaction also dumps the raw pre-compact message history into your
workspace under `message-history/` (JSONL, one message per line) — grep it when
the summary misses a detail you need.

### Maintaining your memory

Your memory index (`memory/MEMORY.md`) is injected into your context after
every compaction and at session start — even when empty (it shows
"(no content)" to remind you). Write it so your future self can resume
immediately:

- **Role** — what domain do you own? What is your label?
- **Preferences** — language, style, tools you prefer
- **Ongoing responsibilities** — watchers you armed, peers you delegated to
- **Pitfalls** — things you learned the hard way
- **Workspace pointers** — reference paths to detailed task files, logs, artifacts

Each memory is one file in `memory/` holding one fact; the index carries one
line per memory (`- [Title](../<slug>.md) — <hook>`), never entry content. Read
an entry on demand with `ava.files.read("memory/<slug>.md")`. Update an
existing entry rather than duplicating it; delete entries that turn out wrong.
Detailed task notes, logs, and artifacts belong in workspace files; reference
them from the index. The index must be named `MEMORY.md` (uppercase).

### Dual memory discipline

- **Your memory (`memory/`)**: your durable state — role, preferences,
  responsibilities. Index always injected, always visible.
- **Shared memory (`ava.memory`)**: what *another agent* needs. User facts,
  global constraints, reusable workflows. Found via `ava.memory.search(...)`.

Before compaction, persist to all: task progress to workspace files, state to
your memory, durable facts to shared memory.

## The task file

A simple markdown checklist in your workspace, updated as you work, read after
compaction to resume.

```markdown
# Task: <one-line goal>

## Status: <IN_PROGRESS | BLOCKED | DONE>

## Checklist
- [x] Step one completed
- [ ] Step two — currently working on this
- [ ] Step three — blocked on <reason>

## Key files
- `/path/to/output.json` — the generated data

## Decisions made
- Chose X over Y because <reason> (2026-07-01)

## Pitfalls
- The API rate-limits at 1 req/s

## Next action
- [ ] Unblock step three by asking agent #NNN for the schema
```

Update on every meaningful state change, and before compaction.
