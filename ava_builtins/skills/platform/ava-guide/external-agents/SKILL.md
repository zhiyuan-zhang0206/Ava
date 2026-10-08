---
name: external-agents
description: "Launches and supervises external coding agents or Ava takeovers. Use when delegating coding work, resuming a session, or replacing the current executor."
---

# Use other agents

Both `claude` (Anthropic) and `codex` (OpenAI) are coding-agent CLIs you can hand
a task to and let plan + execute. Treat either as "another agent". A session
runs in one of **two modes** — as a **delegated worker** you supervise through
files, or as a **takeover** that replaces you while your own execution pauses.
Read *Two modes* first and keep the two apart. For the session primitives
themselves see `ava.shell` (`run` for one-shot; `sessions` — `new` / `send` /
`send_keys` / `capture` / `kill` — for a persistent one) and `ava.watcher`;
don't re-derive those here.

> **Flags and models drift between releases.** Everything below is a snapshot,
> not a contract. Confirm with `claude --help` / `codex exec --help` and `claude
> --version` / `codex --version` on the actual machine before relying on a flag —
> and note a machine may have only one of the two installed.

## Two modes — read this first

The two modes share nothing but the CLIs; mixing them is the known failure.
Pick one before you launch:

- **Mode A — delegated worker.** The default: you hand a long task to the tool
  and keep steering it through the file-driven pattern — a task file you append
  to, a work file the tool rewrites, a supervisor that wakes you. You stay the
  decider; the tool is a worker. Documented under *Mode A* below.
- **Mode B — takeover (impersonation).** The tool *replaces you*: it talks to
  the human through your normal Ava chat and calls Ava capabilities under your
  identity, while your own execution pauses. It is file-less and
  supervisor-less — the briefing travels inline in its launch message, and the
  two of you never run at the same time. Documented under *Mode B* below.

No leakage in either direction: a takeover never reads a task file and never
writes a work file, and no supervisor watches it; a delegated worker's files
have no meaning to a takeover. If you catch yourself wiring files or a watcher
into a takeover, stop — you are following the wrong section.

## Choose the execution mode

Use `ava-workflow` to decide whether delegation serves the task, define its
scope, and set verification criteria. This sub-skill owns the Ava-specific
launch, supervision, resume, and takeover procedures. Check the actual tool's
installed capabilities before choosing it.

These launch scripts run from an Ava agent's execution context. Humans and
external operators can arrange that agent through the CLI; they should not
assume their shell has an Ava agent identity. A takeover executor follows the
project-local `impersonator-guide`, while its launcher follows Mode B here.

## Mode A — file-driven collaboration (the pattern for long tasks)

After choosing a delegated worker, read [delegated workers](references/delegated-workers.md)
for workspace setup, contract delivery, supervision, and handoff. Read only
the chosen tool's launch reference; takeover instructions do not apply.

## Mode B — takeover (impersonation)

The tool **replaces you**: it talks to the human through your normal Ava chat
and calls Ava capabilities under your identity while your execution is paused.
A takeover is **file-less and supervisor-less** — nothing from Mode A applies:

- **The briefing is inline** (`--brief`, verbatim in the launch message): no
  task file, no work file, nothing watches a file.
- **Run it from your own workspace** by default: pass that directory as the
  spawn workspace argument (other locations are allowed, not recommended).
- **Start — the process interrupts you.** When the takeover activates, the
  platform saves your checkpoint and pauses you; the two of you never run at
  the same time, so there is no lockstep and no supervisor.
- **End — one message resumes you.** When the takeover releases, a system note
  resumes you carrying its summary and the handoff JSON path
  (`impersonation/<session_id>.json`). It retains every message, unACKed and
  acknowledged, and an ACK means received, never finished: read it before acting
  on pending human input, and check acknowledged messages for unfinished work.

Launch it from your own execution context, briefing inline:

```bash
.venv/bin/python scripts/spawn_codex.py <workspace-dir> --impersonate-self \
  --impersonation-name 'Fix login' --brief '<the full briefing text>'
```

`--brief` is required; `--tasks-file`/`--work-file` are refused, and no file
watcher runs for a takeover. The same command works with
`spawn_claude.py` and `spawn_dsh.py` ([DeepSeek Harness](references/deepseek_harness.md),
takeover-only). The full procedure is [Let the coding agent take over your
identity](references/impersonate_self.md); the executor's own manual is the
`impersonator-guide` skill.

## After an interruption — resume

A full stop, a reboot, a crash or a TTL expiry closes the shell a coding
session runs in, but the tool keeps its conversation. Every launch prints the
tool's session id (`claude_session=` / `codex_session=`), and
`--resume <id>` on the same spawn command reopens it. Whether to resume or
start fresh is your call from the task's context: [Resume after an
interruption](references/resume_after_interruption.md).

## CLI reference

Flags drift — confirm with `--help` on the actual machine. The per-tool
references carry the launch variants, auth traps and headless notes:

- [Claude Code (`claude`)](references/claude_code.md)
- [OpenAI Codex (`codex`)](references/codex.md)
- [DeepSeek Harness (`dsh`)](references/deepseek_harness.md)
