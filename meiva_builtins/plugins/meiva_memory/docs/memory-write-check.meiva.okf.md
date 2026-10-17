---
type: doc
title: Memory write check — Rule and Duplicate Reminders for Written Notes
description: How meiva_memory checks the notes an agent writes with meiva.files - the files.write/append wraps that record them in the exec subprocess, and the after_exec hook that applies the pool's single-note rules and a duplicate search, then adds at most one reminder note.
tags:
- plugin
- memory
- extensions
---

# Memory write check

## Format

There is no writer function: the agent writes the whole note, frontmatter included,
with `meiva.files.write` (or `append`) under `PATH` or `PERSONAL_PATH`. Nothing is
generated, merged or indexed for it, and `meiva.files` never rewrites the content (it
no longer stamps `injection-risk` into memory notes).

```markdown
---
type: Memory
meiva_agent: <your id>
meiva_machine: <your machine>
timestamp: '2026-01-31T09:30:00+00:00'
title: <short title>
description: <one line>
tags: [type/feedback]
---
<!-- agent-<your id> @ <your machine>, YYYY-MM-DD HH:MM -->
```

Required by the pool validator (`template/validate.py`): `type`, `meiva_agent`,
`description`, exactly one valid `type/<x>` tag; a note stays under 8000 characters.
The cap counts characters as `len()` — the measurement the shared budget names
`Unit.CHARS` — and stays with the standalone validator (which also runs where
`base.*` is unimportable); the write check adds no second cap and only surfaces
its finding.

## Write check

The `meiva_memory` plugin wraps `meiva.files.write` / `append` / `edit` (outside `meiva_code`'s
cwd-tracking wraps of the same targets: plugins stack in name order, the later one is
outermost). After the layer below succeeds, a `.md` path under `PATH` or `PERSONAL_PATH` is
added to the plugin state (`shared_notes` / `personal_notes`, relative paths, union-merged);
the wrap returns the lower layer's result and lets its exception through, and its own
failures are only logged. The exec subprocess sends no request and imports no `httpx` or
decision-model code.

The `after_exec` hook then reads and clears the record and checks, under
`memory_write_check_deadline_seconds` (5s, shared by all notes of the call):

- every written note against `template/validate.py::validate_note` (the single-note rules
  and the 8000-character cap; directory, index and pointer rules stay with the steward);
- every written shared note against the pool: `POST /api/memory/search` with
  `purpose="dedup"`, query = the note's description and body (4000 characters at most),
  `k = 5` (the gateway ranks `memory_dedup_candidates`, default 5, not the 30 of recall/search; the note itself is set aside, so an already indexed note leaves four). The gateway restores the text of its
  closest candidates and a decision model answers, per candidate, whether it records
  essentially the same knowledge (`usage_kind=memory_write_dedup`). Candidates at or above
  `memory_write_dedup_threshold` (0.80, not benchmark-calibrated) are named; every probability
  is logged (`[memory-write-check]`). Personal notes are not searched.

A finding becomes one `memory_write_check` system note after the exec result; no finding
adds nothing. Nothing is blocked, rewritten or raised: a timeout, an unavailable search
or any unexpected error is logged and the notes go unreported.

Every written note — shared and personal — is checked against the pool validator's single-note rules (template/validate.py::validate_note); only the notes with findings are reported, shared notes first, each group in path order, and a note that is gone or is not a note is skipped. Nothing is changed, blocked or rejected: the findings come back as one reminder.

A written shared note is searched against the pool with purpose dedup: the query is the note's description then body, capped at 4000 characters; k = 5, with the note itself set aside, so an already-indexed note leaves four candidates, and only the five closest others are considered. Candidates at or above memory_write_dedup_threshold are named with their probability. Personal notes are not searched.

## Key Dependencies

- [[meiva_builtins/plugins/meiva_memory/docs/memory-api.meiva.okf.md]] — the `meiva.memory` surface the notes live under
- [[meiva_builtins/plugins/meiva_memory/docs/meiva_memory.meiva.okf.md]] — the owning plugin
- [[services/docs/gateway_side/memory_indexer/memory-rerank.meiva.okf.md]] — the `dedup` purpose of the search ranking
