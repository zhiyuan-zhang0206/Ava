---
type: doc
title: Memory index notes — the two stores' standing context notes
description: ava_memory's two index context notes — the shared pool MEMORY.md and the agent's own workspace memory/ index — their builders and framings, the injection guard on the shared index, and the per-agent soft line cap judged through the shared budget component.
tags:
- plugin
- memory
- extensions
---

# Memory index notes

Both stores put an index in front of the agent at the start of every context
window, and only the index — each memory is one file beside it, read on demand.
`notes.py` defines the two builders; `plugin.py` registers them into the
framework's ordered context-note registry, which `init_context` lays down
whenever a window is established. Registration lives in `plugin.py` because a
plugin (re)load re-executes it, while an import of this module hits the
sys.modules cache — decorators here would not re-run. The third memory note, the
`inheritable` blocks read off the ancestor chain, is
[[ava_builtins/plugins/ava_memory/docs/memory-inheritance.ava.okf.md]].

Neither note is core. Disabling `ava_memory` removes the stores, the SDK surface
and these notes together — the agent is then not told about a capability it does
not have. The write discipline both stores share is a system-prompt section
(`plugin.py`), not a note: it is fixed text, where an index is not. Ranks, fork
behavior and the window-side wiring are
[[agent/graph/docs/context-notes/memory-injection.ava.okf.md|Memory Injection]]'s
to state.

## The shared index

`memory_index_note` renders the pool's `MEMORY.md` at `ava.memory.PATH` as one
system note, behind a framing that states location and mechanics only. The text
is scanned with `scan_content` before injection (audit round-2 up-security-trust
P0-2); when flagged, the note keeps the content and prefixes the visible security
warning. The note returns None
when the layer is off (or eval-isolated) and when the index file is missing or
empty.

```ava-contract
id = "memory-index-note"
kind = "invariant"
statement = "The shared index note renders the pool's MEMORY.md (ava.memory.PATH) as one system note; the injected text is scanned with scan_content and, when flagged, is armed with a visible warning prefix while its content is kept; the note is suppressed by eval isolation and by the injection gate."
code = ["ava_builtins/plugins/ava_memory/notes.py:_FRAMING", "ava_builtins/plugins/ava_memory/notes.py:memory_index_note"]
tests = ["ava_builtins/plugins/ava_memory/tests/test_ava_memory_notes.py::test_memory_index_injection_guard", "ava_builtins/plugins/ava_memory/tests/test_ava_memory_notes.py::test_memory_index_note_is_suppressed_for_eval_isolation", "ava_builtins/plugins/ava_memory/tests/test_ava_memory_notes.py::test_memory_index_notes_keep_two_live_roots_separate"]
```

## The per-agent index

`per_agent_memory_note` always returns a note: the agent's own index
(`workspace_dir(agent_id)` + `memory/MEMORY.md`), creating the directory and an
empty index when missing, and rendering the framing plus `(no content)` when the
index is empty. Entry files beside the index are not injected — the agent reads
them on demand. The framing names location and mechanics only and must not claim how
relative paths resolve — the old claim misled agents into writing memory beside
the tracked cwd (audit #577); that statement belongs to the SDK core.

```ava-contract
id = "per-agent-memory-index"
kind = "invariant"
statement = "The per-agent index note reads the agent's own store (workspace_dir(agent_id) + memory/MEMORY.md) — never another agent's — and is suppressed by the layer gate or an unestablished agent id."
code = ["ava_builtins/plugins/ava_memory/notes.py:per_agent_memory_note", "ava_builtins/plugins/ava_memory/notes.py:PER_AGENT_FRAMING", "ava_builtins/plugins/ava_memory/notes.py:_NO_CONTENT", "ava_builtins/plugins/ava_memory/notes.py:_PER_AGENT_MEMORY_DIRNAME"]
tests = ["ava_builtins/plugins/ava_memory/tests/test_ava_memory_notes.py::test_personal_index_uses_hosted_turn_identity", "ava_builtins/plugins/ava_memory/tests/test_ava_memory_notes.py::test_personal_index_skips_unestablished_identity", "ava_builtins/plugins/ava_memory/tests/test_ava_memory_notes.py::test_context_notes_skips_the_stores_that_are_off"]
```

## The per-agent soft cap

Above `memory_per_agent_index_max_lines` a maintenance nudge is appended below
the index — the index is still injected in full; the note only nudges, it never
truncates. The count is the shared line measure and the judgment is the shared
budget's soft threshold: a cap of 0 or less reads as ok, disabling the check.

```ava-contract
id = "per-agent-index-soft-cap"
kind = "invariant"
statement = "Above memory_per_agent_index_max_lines a maintenance nudge is appended below the index — the index is still injected in full (never truncated); the count is the shared line measure and the judgment is the shared budget's soft threshold, with a cap of 0 or less disabling it."
code = ["ava_builtins/plugins/ava_memory/notes.py:_OVER_CAP_NOTE", "ava_builtins/plugins/ava_memory/notes.py:_per_agent_maintenance_suffix"]
tests = ["ava_builtins/plugins/ava_memory/tests/test_ava_memory_notes.py::test_the_over_cap_index_keeps_the_full_body_and_gets_the_nudge", "ava_builtins/plugins/ava_memory/tests/test_ava_memory_notes.py::test_the_index_at_the_cap_gets_no_nudge", "ava_builtins/plugins/ava_memory/tests/test_ava_memory_notes.py::test_a_cap_of_zero_disables_the_nudge", "ava_builtins/plugins/ava_memory/tests/test_ava_memory_notes.py::test_the_cap_counts_lines_not_characters"]
```

## Key Dependencies

- [[ava_builtins/plugins/ava_memory/docs/ava_memory.ava.okf.md]] — the owning plugin
- [[agent/graph/docs/context-notes/memory-injection.ava.okf.md]] — the notes in the window: ranks, switches, fork behavior
- [[base/packages/docs/text-budget.ava.okf.md]] — the shared metric and budget component
