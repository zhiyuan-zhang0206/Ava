---
type: doc
title: Panel write check — Recording To-User Panel Writes
description: How meiva_to_user records panel writes - the transparent files.write / append / edit wraps that record, inside the exec subprocess, whether a call wrote this agent's workspace to_user.md panel, and the state the reminder hook reads.
tags:
- extensions
- plugin
- agent-extension
---

# Panel write check — Recording To-User Panel Writes

This is the recording half of `meiva_to_user`
([[meiva_builtins/plugins/meiva_to_user/docs/meiva_to_user.meiva.okf.md|the soft-limit reminder]]
is the other): the plugin's `files.write` / `append` / `edit` wraps record, inside
the exec subprocess, whether a call wrote this agent's panel, and the reminder's
`after_exec` hook reads that record in the agent process. Nothing here blocks,
rewrites or rejects a write.

## SDK wraps

`files.write` / `files.append` / `files.edit` — transparent (call the layer below
exactly once; hand back its result or let its exception propagate) and record only
after a write that succeeded. `meiva_builtins/plugins/meiva_to_user/written.py:panel_path` matches only this agent's
`<workspace>/to_user.md` (relative paths resolve against the tracked `meiva.cwd` like the
core); shell-redirected writes are not observed, the same contract the memory write
check has. Wrap order: plugins stack in name order, later outermost — `meiva_to_user`
sits outside `meiva_memory`'s and `meiva_code`'s wraps of the same targets.

The record is one pending flag, `MeivaToUserState.panel_written`; the hook always clears
it before deciding.

## Contracts

The files.write / append / edit wraps call the layer below exactly once and return or propagate its outcome untouched; a panel write is recorded only after a successful call, and recording never raises. panel_path matches only this agent's <workspace>/to_user.md; the wraps see the caller's raw path, so a relative one resolves against the tracked meiva.cwd. The wraps carry no docstrings, so the rendered SDK keeps the original files.* contracts.
