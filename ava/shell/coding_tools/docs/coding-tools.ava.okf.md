---
type: doc
title: ava.shell.coding_tools — Coding-tool launchers
description: The launch logic behind the ava-use-other-agents skill's spawn scripts. It starts Claude Code or Codex inside an ava.shell persistent session and delivers the first message, either as a supervised worker or as a takeover that impersonates the launching agent.
tags:
- shell
- lifecycle
---

# ava.shell.coding_tools — Coding-tool launchers

## What it is

This package is the implementation behind the
`ava_builtins/skills/coordination/ava-use-other-agents/scripts/spawn_claude.py` and
`spawn_codex.py` command-line entries. Each launcher does the same things:

1. Opens an `ava.shell.sessions` PTY.
2. Starts the tool in that PTY.
3. Waits for the tool's UI to be ready.
4. Delivers the first message. For a supervised worker (Mode A) that is the
   collaboration contract. For a takeover (Mode B) it is an inline briefing.
5. Checks that the message was actually submitted.

Each launch prints the tool's own session id (`claude_session`, chosen up
front with `--session-id`; `codex_session`, read from `/status`). The tool's
conversation store outlives the shell, so `resume` reopens exactly that session
after a stop, reboot or crash closed it.

It lives under `ava.shell` because it drives these same session primitives.
It is not part of the agent-facing surface: the package appears in no
`__all_for_ava__`. The scripts keep only argument parsing. Each passes its own
skill directory, whose `references/` holds the collaboration contract (and
locates the impersonator guide) and whose `scripts/` holds the supervisor
script (`watch_work.py`) and the resident relay plugin (`ava-relay/`).

## Modules

- `claude` — the Claude Code launcher. A supervised worker is not registered
  and supervises through the skill's `watch_work.py`. A takeover is a generation
  of its own under `(cluster, workspace, claude)`. Its relay starts with the
  session through the resident plugin, unless the executor-armed flow is
  requested.
- `codex` — the Codex launcher. Every launch is a generation of its own under
  `(cluster, workspace, codex)`, so several can share a workspace. It adds an automatic supervisor for the
  supervised mode and, for a takeover, the shared app server the TUI and the
  relay both use. Codex runs on the user's own `~/.codex` with per-session
  `-c` overrides (workspace trust, no startup update check).
- `_claude_checks` — Claude-specific checks:
  - UI readiness and the missing-executable and signed-out markers;
  - bracketed-paste delivery (#4364);
  - the generation-scoped relay stub;
  - start-receipt verification from the session transcript.
- `_first_run` — presets Claude Code's first-run dialogs so an unattended spawn
  never parks on one.
- `_common` — shared by both launchers:
  - workspace path resolution;
  - a new owner generation for each launch, after the workspace's dead ones are
    reclaimed;
  - owner-record status (every generation of the workspace) and cancel.

## Key dependencies
- [[ava/shell/docs/shell.ava.okf.md|ava.shell]] — the session primitives every launch drives
- [[base/sessions/docs/coding-session-owner.ava.okf.md]] — per-launch generations, the dead-sibling sweep, and exact cleanup
- [[ava_builtins/skills/coordination/ava-use-other-agents/docs/ava-use-other-agents.ava.okf.md]] — the skill whose scripts are the command-line entries
