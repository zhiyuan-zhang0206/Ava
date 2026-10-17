---
type: doc
title: rebuild_memory_index — Pool Index Rebuild
description: "skills/scripts/rebuild_memory_index.py rewrites the pool's `## Pointers` section from the notes on disk — one line per root note (full description) and one per topic directory — while the index cap stays with the pool's pre-commit hook."
tags:
- extensions
- agent-instruction
---

# rebuild_memory_index — Pool Index Rebuild

`meiva_builtins/plugins/meiva_memory/skills/scripts/rebuild_memory_index.py` runs inside the pool checkout (`python3 rebuild_memory_index.py`, normally reached through the steward / `meiva memory` path; see [[meiva_builtins/plugins/meiva_memory/skills/consolidation/docs/consolidation.meiva.okf.md|consolidation]]): it rebuilds `MEMORY.md` from the notes on disk, so the index never depends on a writer remembering to update it.

## Pointer layout

The directory-index ruling of 2026-08-06 gives `## Pointers` two kinds of line and nothing else: one line per root note — `- [title](rel) — description`, the description carried through in full, never truncated — and one line per topic directory — `- [dir/](dir/index.md) — n notes`, with a per-subdirectory count when the directory nests. Links get deeper as structure does.

Everything above the heading is preserved; the rebuild cuts at the exact heading line (`^## Pointers[ \t]*$`), so the template intro's prose mention of `## Pointers` does not end the preamble.

Notes are read tolerantly: the `RESERVED` root files (`MEMORY.md`, `AGENTS.md`, `index.md`, `log.md`) never enter the index, and a note with absent or malformed frontmatter is still indexed — file name as title, empty description — rather than dropped.

## Caps

The index cap is owned by the pool's pre-commit hook, not this script: `MEIVA_MEMORY_INDEX_CAP`, default 16000 characters (≈200 pointer lines, the Claude Code auto-memory scale family — the shared value set by the text-budget work, task #5189). The per-note cap (`MEIVA_MEMORY_NOTE_CAP`, default 8000, enforced by the same hook and `validate.py`) keeps one note to one atomic fact; oversized material moves to the Vault with a pointer note left behind.

```meiva-contract
id = "indexed-entries"
kind = "invariant"
statement = "collect indexes every note file under the pool except the RESERVED root files (MEMORY.md, AGENTS.md, index.md, log.md), skipping .git and .githooks; frontmatter reads title and description from a note's YAML block and degrades to the file name and an empty description when the block is absent or malformed, so a note is indexed with what it carries rather than dropped."
code = ["meiva_builtins/plugins/meiva_memory/skills/scripts/rebuild_memory_index.py:frontmatter", "meiva_builtins/plugins/meiva_memory/skills/scripts/rebuild_memory_index.py:collect"]
tests = ["meiva_builtins/plugins/meiva_memory/skills/scripts/tests/test_rebuild_memory_index.py::test_the_rebuild_writes_one_pointer_line_per_note_and_directory"]
```

```meiva-contract
id = "pointer-rebuild"
kind = "invariant"
statement = "The module rewrites MEMORY.md in place: everything up to the exact `## Pointers` heading line is preserved (a prose mention does not match), followed by one `- [title](rel) — description` line per root note and one `- [dir/](dir/index.md) — n notes` line per topic directory with subdirectory counts; the index cap is enforced by the pool's pre-commit hook (MEIVA_MEMORY_INDEX_CAP, default 16000), not by this script."
code = ["meiva_builtins/plugins/meiva_memory/skills/scripts/rebuild_memory_index.py:heading"]
tests = ["meiva_builtins/plugins/meiva_memory/skills/scripts/tests/test_rebuild_memory_index.py::test_the_rebuild_writes_one_pointer_line_per_note_and_directory", "meiva_builtins/plugins/meiva_memory/skills/scripts/tests/test_rebuild_memory_index.py::test_a_pool_without_an_index_file_gets_one", "meiva_builtins/plugins/meiva_memory/skills/scripts/tests/test_rebuild_memory_index.py::test_a_missing_heading_appends_after_the_existing_preamble"]
```
