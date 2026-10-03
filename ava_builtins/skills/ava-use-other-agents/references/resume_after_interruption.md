# Resume a coding session after an interruption

A Claude Code or Codex session runs inside one of your persistent shells. Some
events end that shell and the coding process in it:

- `ava stop` or `ava restart` (updates included);
- a host reboot;
- a crash, including a crash of the pty-sessions service;
- the shell's TTL expiring;
- an explicit kill.

An agent or gateway restart keeps persistent shells alive, so after one
the session is usually still running. If it is still listed in
`ava.shell.sessions.list()`, it is alive: `capture` it and nudge it. Do not
resume it.

## What survives

- **The tool's own conversation.** Claude Code keeps it under
  `~/.claude/projects/`, and Codex under `~/.codex/`. Both are keyed by a
  session id. Reopening that id restores the tool's whole history: what it read
  and tried, and what it decided.
- **The workspace.** Its files and Git state, and for a supervised worker its
  task file and work file.
- **For a takeover, the handoff JSON** (`impersonation/<session_id>.json`),
  which retains every message and operation.

## How you notice

Your own history shows the interruption. The cluster's lifecycle notes mark
the stop and the restart, and then the coding session is gone:

- `ava.shell.sessions.list()` no longer lists it, and `capture` refuses it;
- a Claude worker's watcher ends with its exit notice;
- a Codex generation's supervisor terminalizes it;
- a takeover ends with the system note `Impersonation <n> ... stopped — the
  executor process is gone` plus the handoff JSON path.

## Whether to resume

This is your judgment, made from the task's context. Resuming is worth it when:

- the work was cut off mid-flight;
- the tool's context would be costly to rebuild: what it read, what it tried,
  and why.

A fresh launch is better when:

- the work was done;
- the task has changed;
- the old context would mislead, for example because the code moved on
  underneath it.

A supervised worker's task file and work file, and a takeover's handoff JSON,
carry what a fresh session needs either way.

## Find the session id

- **The launch printed it:** `claude_session=<uuid>` from `spawn_claude.py`, or
  `codex_session=<uuid>` from `spawn_codex.py`. Look it up in your own history.
  A live Codex session also shows it under `Session:` in `/status`.
- **Failing that, ask the tool, from the workspace directory.**
  - `claude --resume` and `codex resume` open pickers scoped to that directory.
  - `claude --continue` and `codex resume --last` take the most recent session
    there, which is only right when that session was the one you ran.

## Resume

Run the same spawn command again with `--resume <uuid>`: the same workspace,
and for a worker the same task and work files. The tool reopens its own
history, and the first message it receives tells it the session was
interrupted.

**Supervised worker (Mode A).**
- For Claude, start the watcher again (`watch_work.py`). The old one ended with
  the shell.
- For Codex, the launcher starts a new supervisor itself.

**Takeover (Mode B).**
- Read the handoff JSON first.
- Relaunch with `--impersonate-self --resume <uuid> --brief '...'`. Write the
  brief for a session that already knows the task:
  - which takeover session ended, and why (the system note says);
  - the handoff JSON path;
  - what is pending.
- The resumed executor keeps its context, but its old impersonation session is
  over. It starts a new one with the request command in its launch message,
  and catches up on missed messages from the handoff JSON.

**DeepSeek Harness takeovers** cannot be resumed through `spawn_dsh.py` yet.
Launch a fresh takeover whose brief is built from the handoff JSON.

**By hand**, from the workspace directory:
- `claude --resume <uuid> --dangerously-skip-permissions`;
- `codex resume <uuid> --dangerously-bypass-approvals-and-sandbox`, plus the
  per-session `-c` overrides in [Codex](codex.md).
