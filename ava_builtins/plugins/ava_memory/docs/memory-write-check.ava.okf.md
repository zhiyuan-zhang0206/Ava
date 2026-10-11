---
type: doc
title: Memory write check — Rule and Duplicate Reminders for Written Notes
description: How ava_memory checks the notes an agent writes with ava.files - the files.write/append wraps that record them in the exec subprocess, and the after_exec hook that applies the pool's single-note rules and a duplicate search, then adds at most one reminder note.
tags:
- plugin
- memory
- extensions
---

# Memory write check

## Format

There is no writer function: the agent writes the whole note, frontmatter included,
with `ava.files.write` (or `append`) under `PATH` or `PERSONAL_PATH`. Nothing is
generated, merged or indexed for it, and `ava.files` never rewrites the content (it
no longer stamps `injection-risk` into memory notes).

```markdown
---
type: Memory
ava_agent: <your id>
ava_machine: <your machine>
timestamp: '2026-01-31T09:30:00+00:00'
title: <short title>
description: <one line>
tags: [type/feedback]
---
<!-- agent-<your id> @ <your machine>, YYYY-MM-DD HH:MM -->
```

Required by the pool validator (`template/validate.py`): `type`, `ava_agent`,
`description`, exactly one valid `type/<x>` tag; a note stays under 8000 characters.

## Write check

The `ava_memory` plugin wraps `ava.files.write` / `ava.files.append` (outside `ava_code`'s
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
  `k = 6` so that the note itself can be set aside. The gateway restores the text of its
  closest candidates and a decision model answers, per candidate, whether it records
  essentially the same knowledge (`usage_kind=memory_write_dedup`). Candidates at or above
  `memory_write_dedup_threshold` (0.80, not benchmark-calibrated) are named; every probability
  is logged (`[memory-write-check]`). Personal notes are not searched.

A finding becomes one `memory_write_check` system note after the exec result; no finding
adds nothing. Nothing is blocked, rewritten or raised: a timeout, an unavailable search
or any unexpected error is logged and the notes go unreported. `ava.files.edit` is not
wrapped, so an in-place edit of an existing note is not re-checked.


## Key Dependencies

- [[ava_builtins/plugins/ava_memory/docs/memory-api.ava.okf.md]] — the `ava.memory` surface the notes live under
- [[ava_builtins/plugins/ava_memory/docs/ava_memory.ava.okf.md]] — the owning plugin
- [[services/docs/gateway_side/memory_indexer/memory-rerank.ava.okf.md]] — the `dedup` purpose of the search ranking
