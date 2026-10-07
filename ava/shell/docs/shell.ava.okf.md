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
- `run_background(cmd, *, name, cwd=None, keep=False, ttl, notify=None) → BackgroundRun` — Run a long command in a new persistent session, return immediately, then the session auto-closes. `name` is a lowercase slug; `page-` names are reserved for pages. Omitted `notify` applies the target agent's `completion_notice_policy`: `all` (the default) preserves one completion message per exit and `hourly` emits a restart-safe hourly digest of every completion. Explicit `notify="always"` overrides that agent policy for this run. The message carries only the log path and the last 3 output lines (no exit status; read the tail). Output is streamed to `.shell_logs/<sid>_<name>.log` in workspace (relative path, can read/grep to view progress) and visible live in the session capture. `keep=True` retains the session after command ends. `ttl` max 86400s (24 hours). Use for one-shot long tasks like build/test/download; interactive programs still use `sessions`. Returns `BackgroundRun(session_id, output_path)`.

### Persistent Sessions (`ava.shell.sessions`)
- `new(name: str, *, ttl) → int` — Create a named session (name is a lowercase slug; `page-` names are reserved for pages), return session ID. `ttl` (seconds) is **required** (max 86400 = 24 hours — sessions live at most one day): the gateway force-kills the session when it elapses, so pass a large value for a long-resident session.
- `send(id, cmd, *, enter=True)` — Send command to session. Asynchronous—returns without waiting for command completion. `enter=False` only types without submitting.
- `send_keys(id, *keys)` — Send raw keystrokes (e.g., `C-c`, `Escape`, `Up`, `Enter`).
- `capture(id, lines=200, *, scrollback=True) → str` — Read the last N lines of output. `scrollback=False` only captures the currently visible screen (for full-screen TUI programs, `lines` is ignored).
- `kill(id)` — Terminate session.
- `list() → dict[int, str | None]` — List your sessions (id → name, unnamed as None).

## Key Dependencies
- [[agent/docs/sessions.ava.okf.md]] — the session backend is the underlying implementation of sessions
- [[ava/shell/coding_tools/docs/coding-tools.ava.okf.md|ava.shell.coding_tools]] — launchers that start Claude Code / Codex in these sessions (not agent-facing)

## Notes
Sessions retain cwd, environment variables, and background processes, used to drive interactive CLI tools like Claude Code, Codex. Sessions outside the spawning checkout, including paths under its `.worktrees/` or `.claude/worktrees/` sibling-worktree directories, receive the venv-prefixed PATH but omit `VIRTUAL_ENV`, preventing a bare uv command from selecting the spawning checkout's venv. The shell is a login shell (`bash -l -i`), so the user's profile may still reorder PATH; the home rides `AVA_HOME` (else `~/.ava`), which a bare `ava` (the host's link to the production CLI) reads. Sessions survive after the agent process exits (not reclaimed with the process) and across agent, agent-host, and gateway restarts — the machine's `pty-sessions` service holds them, so only its own `kill`, a terminate of its agent with `kill_all_shell_sessions` (silent), its shell exiting, its TTL expiring, `ava stop` or `ava restart` (which close terminals; so do updates), a restart or crash of that service, or a machine reboot ends it; watchers are also special sessions and appear in `sessions.list()`. TTL reclamation notifies the owner only when it interrupts a running job; an empty shell's reaping is silent.

The SDK, including Codex/Claude launchers and watchers, supplies
`forward_env_dict(activate_venv=...)`. `PtySessionBackend.new_session` builds
the same cwd-based projection when `env` is omitted; an explicit empty dict
stays empty. The PTY shell fork clears inherited `VIRTUAL_ENV` before applying
the projection, so a launcher or permissions-helper host cannot restore an
omitted activation. Other ambient variables remain inherited; explicit
envfile activation and watcher runner overrides still win. This is a
creation-time rule; a later `cd` does not change it.

Page servers opened with `ava.ui.serve` run in your own sessions (`page-<name>`, one entry per open page; the label grammar is owned by `base/sessions/page_session.py`) and appear in `sessions.list()`; see the [[ava/docs/ui.ava.okf.md]] server lifecycle for closure through `close()` or TTL expiry — killing the entry does not close the page, and a terminate's `kill_all_shell_sessions` spares them.
