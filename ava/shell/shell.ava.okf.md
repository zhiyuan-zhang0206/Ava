---
type: doc
title: ava.shell — Shell Operations
description: "Interface for executing shell commands. Three modes: one-shot run(), background run_background() (auto-report on completion), persistent sessions."
tags: []
---

# ava.shell — Shell Operations

## What it is

Interface for executing shell commands. Three modes: one-shot `run()`, background `run_background()`, persistent sessions `sessions`.

## Core API

### One-shot
- `run(cmd, *, cwd=None, timeout=30.0) → ShellResult` — Run command, return stdout as a string with the exit status attached: read-only `.returncode` (0 = success, non-zero = failure) and `.stderr`. String operations on the result return a plain `str` without these fields. Non-zero exit does not raise an exception; after `timeout` seconds kill the command and raise `subprocess.TimeoutExpired`. Default working directory is agent's workspace.

### Background (auto-report on completion)
- `run_background(cmd, *, name, cwd=None, keep=False, ttl) → BackgroundRun` — Run a long command in a new persistent session, return immediately, send you a message when the command exits (exit code + log path + output tail), then the session auto-closes. Output is streamed to `.shell_logs/<sid>_<name>.log` in workspace (relative path, can read/grep to view progress) and visible live in the session capture. `keep=True` retains the session after command ends. `ttl` max 86400s (24 hours). Use for one-shot long tasks like build/test/download; interactive programs still use `sessions`. Returns `BackgroundRun(session_id, output_path)`.

### Persistent Sessions (`ava.shell.sessions`)
- `new(name: str, *, ttl) → int` — Create a named session (name is a lowercase slug), return session ID. `ttl` (seconds) is **required** (max 86400 = 24 hours — sessions live at most one day): the gateway force-kills the session when it elapses, so pass a large value for a long-resident session.
- `send(id, cmd, *, enter=True)` — Send command to session. Asynchronous—returns without waiting for command completion. `enter=False` only types without submitting.
- `send_keys(id, *keys)` — Send raw keystrokes (e.g., `C-c`, `Escape`, `Up`, `Enter`).
- `capture(id, lines=200, *, scrollback=True) → str` — Read the last N lines of output. `scrollback=False` only captures the currently visible screen (for full-screen TUI programs, `lines` is ignored).
- `kill(id)` — Terminate session.
- `list() → dict[int, str | None]` — List your sessions (id → name, unnamed as None).

## Key Dependencies
- [[sessions.ava.okf.md]] — the session backend is the underlying implementation of sessions

## Notes
Sessions retain cwd, environment variables, and background processes, used to drive interactive CLI tools like Claude Code, Codex. Sessions outside the spawning checkout, including paths under its `.worktrees/` or `.claude/worktrees/` sibling-worktree directories, retain the venv-prefixed PATH but omit `VIRTUAL_ENV`, preventing a bare uv command from selecting the spawning checkout's venv. Sessions survive after the agent process exits (not reclaimed with the process) and across cluster restarts/updates — each runs in its own detached host process, so only its own `kill`, its shell exiting, its TTL expiring, or a machine reboot ends it; watchers are also special sessions and appear in `sessions.list()`. TTL reclamation notifies the owner only when it interrupts a running job; an empty shell's reaping is silent.
