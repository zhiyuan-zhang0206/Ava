---
type: doc
title: ava_to_user — To-User Panel Soft-Limit Reminder
description: '`ava_to_user` reminds the agent when the workspace `to_user.md` panel — the Markdown context displayed beside its messages — exceeds the soft word limit. Links and images render for the user, so detail belongs in linked files.'
tags:
- extensions
- plugin
- agent-extension
---

# ava_to_user — To-User Panel Soft-Limit Reminder

## What is it

`ava_to_user` keeps the workspace `to_user.md` panel — the Markdown document the user
reads beside the agent's messages ([Markdown shared context decision](../../../../docs/decisions/frontend/documents/2026-10-15-markdown-context-and-file-references.md)) —
within its soft limit. It is the second member of the write-check family
([[ava_builtins/plugins/ava_memory/docs/memory-write-check.ava.okf.md]] is the first):
the plugin's `files.write` / `append` / `edit` wraps record, inside the exec subprocess,
whether a call wrote this agent's panel (`written.py`; [[ava_builtins/plugins/ava_to_user/docs/panel-write-check.ava.okf.md|the recording's own node]]), and the after_exec hook
(`agent_runtime.py`) reads that record in the agent process, counts the panel in
CJK-aware words and adds one reminder note when it exceeds `to_user_soft_limit_words`.
The check is soft: it never blocks, rewrites or rejects a write, and it never fails the
turn — a panel that cannot be read is logged and skipped.

## Declared contributions

### after_exec hook

`_ToUserLimitHook` runs after an exec call. No-op, with no state read, when no panel
write was recorded; otherwise it always clears the record and then:

- reads the panel through the shared read face
  (`base/agents/to_user/document.py:read_document`) off the event loop;
- counts it (`counting.py`) and compares against `to_user_soft_limit_words`
  (read from the agent's slices, so per-agent pins apply);
- over the limit: adds one `system_note_message` tagged
  `NoteTag.TO_USER_SOFT_LIMIT` carrying the current count, the limit and the guidance
  (links and images render for the user like a normal Markdown editor, so detail can
  live in linked files; a small task can carry everything on the page, a growing one
  layers it);
- back within the limit: adds nothing and re-arms the reminder;
- skips entirely under `eval_isolation`.

Cadence is `to_user_limit_reminder_cadence`: `once_per_compaction` (default) reminds at
most once per context window, re-armed by a compaction (the same `reminded` set +
`last_seen_compact` bookmark `ava_sdk_reminder` uses) and by the panel coming back
within its limit; `every_time` reminds on every over-limit write.

### State

`AvaToUserState` (`plugins.ava_to_user`): `panel_written` (pending record),
`reminded` (once-scoped categories already pointed out this window), `last_seen_compact`
(the compaction re-arm bookmark).

## Configuration

- `to_user_soft_limit_words` (`AVA_TO_USER_SOFT_LIMIT_WORDS`, default 1500, per-agent,
  live): the soft limit in CJK-aware words — every CJK character counts one, every run
  of Latin letters/digits counts one (`ava_builtins/plugins/ava_to_user/counting.py:count_panel_words`), so English reads
  roughly as words.
- `to_user_limit_reminder_cadence` (`AVA_TO_USER_LIMIT_REMINDER_CADENCE`, default
  `once_per_compaction`, per-agent, live).

## Key dependencies

- [[docs/plugins/declared-contributions.ava.okf.md]] — the contribution surfaces this
  plugin declares (wraps, hooks, state, config)
- [[docs/plugins/graph-edge-hooks.ava.okf.md]] — the after_exec hook contract
- [[ava_builtins/plugins/ava_memory/docs/memory-write-check.ava.okf.md]] — the
  write-check family's other member and the wrap/cadence precedents

## Notes

- The panel's word count is a policy unit of this reminder, not a property of the
  document format: `base/agents/to_user/` (read face, reference resolution, size caps)
  is unchanged.
- The hard caps stay: 256 KiB per Markdown document, 256 local references, 8 reference
  hops ([Markdown shared context decision](../../../../docs/decisions/frontend/documents/2026-10-15-markdown-context-and-file-references.md)).

## Contracts

```ava-contract
id = "soft-limit-reminder"
kind = "invariant"
statement = """The after_exec check is a no-op without a recorded write; otherwise it always clears the record, reads the panel off-loop and compares against to_user_soft_limit_words: over the limit it adds one NoteTag.TO_USER_SOFT_LIMIT note carrying the count and the limit, back within the limit it re-arms silently. It never blocks, rewrites or rejects a write and never fails the turn (unreadable panel -> logged and skipped; eval_isolation skips). once_per_compaction (default) reminds at most once per window, re-armed by a compaction and by coming back within the limit; every_time reminds on every over-limit write."""
code = ["ava_builtins/plugins/ava_to_user/agent_runtime.py:_ToUserLimitHook", "ava_builtins/plugins/ava_to_user/_state.py:AvaToUserState"]
tests = ["ava_builtins/plugins/ava_to_user/tests/test_to_user_limit_hook.py::test_no_recorded_write_is_a_noop", "ava_builtins/plugins/ava_to_user/tests/test_to_user_limit_hook.py::test_an_over_limit_panel_gets_one_reminder_with_both_figures", "ava_builtins/plugins/ava_to_user/tests/test_to_user_limit_hook.py::test_once_per_compaction_rearms_after_a_compaction"]
```

```ava-contract
id = "cjk-aware-count"
kind = "invariant"
statement = """Panel words are counted CJK-aware: every CJK character counts one; every maximal run of letters/digits (underscore excluded) counts one; punctuation and whitespace count nothing. Runs are matched on the CJK-blanked text so a run never merges across scripts. A flat character count is deliberately not used: it misreads CJK text."""
code = ["ava_builtins/plugins/ava_to_user/counting.py:count_panel_words", "ava_builtins/plugins/ava_to_user/counting.py:_CJK_CHARACTER"]
tests = ["ava_builtins/plugins/ava_to_user/tests/test_counting.py::test_pure_chinese_counts_by_character", "ava_builtins/plugins/ava_to_user/tests/test_counting.py::test_mixed_chinese_and_english_sums_both"]
```
