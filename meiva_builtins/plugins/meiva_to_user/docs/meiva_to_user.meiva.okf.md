---
type: doc
title: meiva_to_user — To-User Panel Soft-Limit Reminder
description: '`meiva_to_user` reminds the agent when the workspace `to_user.md` panel — the Markdown context displayed beside its messages — exceeds the soft word limit. Links and images render for the user, so detail belongs in linked files.'
tags:
- extensions
- plugin
- agent-extension
---

# meiva_to_user — To-User Panel Soft-Limit Reminder

## What is it

`meiva_to_user` keeps the workspace `to_user.md` panel — the Markdown document the user
reads beside the agent's messages ([Markdown shared context decision](../../../../docs/decisions/frontend/documents/2026-10-15-markdown-context-and-file-references.md)) —
within its soft limit. It is the second member of the write-check family
([[meiva_builtins/plugins/meiva_memory/docs/memory-write-check.meiva.okf.md]] is the first):
the plugin's `files.write` / `append` / `edit` wraps record, inside the exec subprocess,
whether a call wrote this agent's panel (`written.py`; [[meiva_builtins/plugins/meiva_to_user/docs/panel-write-check.meiva.okf.md|the recording's own node]]), and the after_exec hook
(`agent_runtime.py`) reads that record in the agent process, counts the panel through the
shared word metric and adds one reminder note when it exceeds `to_user_soft_limit_words`.
The check is soft: it never blocks, rewrites or rejects a write, and it never fails the
turn — a panel that cannot be read is logged and skipped.

## Declared contributions

### after_exec hook

`_ToUserLimitHook` runs after an exec call. No-op, with no state read, when no panel
write was recorded; otherwise it always clears the record and then:

- reads the panel through the shared read face
  (`base/agents/to_user/document.py:read_document`) off the event loop;
- counts it through the shared `count_words` (`base/packages/text_budget.py`) and
  compares against `to_user_soft_limit_words`
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
`last_seen_compact` bookmark `meiva_sdk_reminder` uses) and by the panel coming back
within its limit; `every_time` reminds on every over-limit write.

### State

`MeivaToUserState` (`plugins.meiva_to_user`): `panel_written` (pending record),
`reminded` (once-scoped categories already pointed out this window), `last_seen_compact`
(the compaction re-arm bookmark).

## Configuration

- `to_user_soft_limit_words` (`MEIVA_TO_USER_SOFT_LIMIT_WORDS`, default 1500, per-agent,
  live): the soft limit in CJK-aware words — every CJK character counts one, every run
  of Latin letters/digits counts one (the shared `count_words`,
  [[base/packages/docs/text-budget.meiva.okf.md]]), so English reads roughly as words.
- `to_user_limit_reminder_cadence` (`MEIVA_TO_USER_LIMIT_REMINDER_CADENCE`, default
  `once_per_compaction`, per-agent, live).

## Key dependencies

- [[docs/plugins/declared-contributions.meiva.okf.md]] — the contribution surfaces this
  plugin declares (wraps, hooks, state, config)
- [[docs/plugins/graph-edge-hooks.meiva.okf.md]] — the after_exec hook contract
- [[meiva_builtins/plugins/meiva_memory/docs/memory-write-check.meiva.okf.md]] — the
  write-check family's other member and the wrap/cadence precedents

## Notes

- The panel's word count is a policy unit of this reminder, not a property of the
  document format: `base/agents/to_user/` (read face, reference resolution, size caps)
  is unchanged.
- The read-side caps are config (`settings.to_user`): by default 1 MiB per Markdown
  document, 1024 local references, 16 reference hops and 64 MiB per referenced file
  ([limits decision](../../../../docs/decisions/frontend/documents/2026-10-17-to-user-panel-limits-configurable.md)).

## Contracts

The after_exec check is a no-op without a recorded write; otherwise it always clears the record, reads the panel off-loop and compares against to_user_soft_limit_words: over the limit it adds one NoteTag.TO_USER_SOFT_LIMIT note carrying the count and the limit, back within the limit it re-arms silently. It never blocks, rewrites or rejects a write and never fails the turn (unreadable panel -> logged and skipped; eval_isolation skips). once_per_compaction (default) reminds at most once per window, re-armed by a compaction and by coming back within the limit; every_time reminds on every over-limit write.
