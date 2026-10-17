---
type: doc
title: meiva.shell.coding_tools — Coding-tool launchers
description: The launch logic behind the meiva-guide.external-agents skill's spawn scripts. It starts Claude Code, Codex, or Antigravity inside an meiva.shell persistent session and delivers the first message, either as a supervised worker or with a custom inline prompt.
tags:
- shell
- lifecycle
---

# meiva.shell.coding_tools — Coding-tool launchers

## What it is

This package is the implementation behind the
`meiva_builtins/skills/platform/meiva-guide/external-agents/scripts/spawn_claude.py`,
`spawn_codex.py`, and `spawn_antigravity.py` command-line entries. Each launcher
does the same things:

1. Opens an `meiva.shell.sessions` PTY.
2. Starts the tool in that PTY.
3. Waits for the tool's UI to be ready.
4. Delivers the first message. Without `--prompt` (Mode A) that is the
   collaboration contract. With `--prompt` (Mode B or custom prompt) it is the
   inline prompt verbatim.
5. Checks that the message was actually submitted.

Each launch prints the tool's own session id (`claude_session`, chosen up
front with `--session-id`; `codex_session`, read from `/status`; `antigravity_session`,
exported as `ANTIGRAVITY_CONVERSATION_ID`). The tool's conversation store
outlives the shell, so `resume` reopens exactly that session after a stop,
reboot or crash closed it.

It lives under `meiva.shell` because it drives these same session primitives.
It is not part of the agent-facing surface: the package appears in no
`__all_for_meiva__`. The scripts keep only argument parsing. Each passes its own
skill directory, whose `references/` holds the collaboration contract and whose
`scripts/` holds the supervisor script (`watch_work.py`).

## Modules

- `claude` — the Claude Code launcher. Without `--prompt`, creates `tasks.md`
  and `work.md` and supervises through the skill's `watch_work.py`. With
  `--prompt`, sends the inline prompt without creating task or work files.
- `codex` — the Codex launcher. Every launch is a generation of its own under
  `(cluster, workspace, codex)`, so several can share a workspace. It starts the
  shared app server (`codex app-server --listen`) and connects the TUI with
  `--remote`, and starts an automatic supervisor when `--prompt` is not passed.
  Codex runs on the user's own `~/.codex` with per-session `-c` overrides
  (workspace trust, no startup update check).
- `antigravity` — the Antigravity (`agy`) launcher. Opens `agy chat -a <workspace>`
  in a persistent PTY, exports `ANTIGRAVITY_CONVERSATION_ID`, and sends either
  the file-driven collaboration contract or the inline `--prompt`.
- `_claude_checks` — Claude-specific checks:
  - UI readiness and the missing-executable and signed-out markers;
  - bracketed-paste delivery (#4364);
  - start-receipt verification from the session transcript.
- `_first_run` — presets Claude Code's first-run dialogs so an unattended spawn
  never parks on one.
- `_common` — shared by the launchers:
  - workspace path resolution;
  - a new owner generation for each launch, after the workspace's dead ones are
    reclaimed;
  - owner-record status (every generation of the workspace) and cancel.

## Key dependencies
- [[meiva/shell/docs/shell.meiva.okf.md|meiva.shell]] — the session primitives every launch drives
- [[base/sessions/docs/coding-session-owner.meiva.okf.md]] — per-launch generations, the dead-sibling sweep, and exact cleanup
- [[meiva_builtins/skills/platform/meiva-guide/external-agents/docs/external-agents.meiva.okf.md]] — the skill whose scripts are the command-line entries
