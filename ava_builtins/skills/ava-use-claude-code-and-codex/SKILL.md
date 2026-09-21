---
name: ava-use-claude-code-and-codex
description: Drives Claude Code or Codex CLI as supervised long-running coding agents. Use when outsourcing multi-step implementation or review, choosing between the two CLIs, resuming a coding session, monitoring delegated coding work, or launching either CLI to impersonate the launching Ava agent.
---

# Use Claude Code and Codex

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

## When to outsource, and to which

| Situation | Choice |
|-----------|--------|
| Simple file read/write, grep, git, a single command | Do it yourself / your own tools |
| Multi-step coding task (write + test + fix) you can describe completely | Outsource |
| Expected < 5 files, < 100 lines, and you know the codebase | Do it yourself |
| Expected multiple trial-and-error rounds, > 10 files | Outsource (Claude Code, file-driven) |
| Need a specific model's reasoning / long context | Pick the tool backed by that model |
| Code review of a diff | `codex exec review`, or `claude -p` with a review prompt |
| You need tight control over each step | Do it yourself |

## Mode A — file-driven collaboration (the pattern for long tasks)

**Persistent session is the default.** Start it in `ava.shell.sessions` and steer
it across many turns — everything below builds on this. Headless one-shot
(`claude -p`, `codex exec`) exists but is rarely needed; avoid it unless the task
is truly self-contained and needs no supervision. Per-tool launch detail lives in
the [Claude Code](reference/claude_code.md) and [Codex](reference/codex.md)
references.

Don't supervise by reading the agent's screen. The screen is a redraw you have to
parse and it vanishes on restart; a file the agent writes is structured, durable,
and diffable. Make a shared work folder the interface and demote keystrokes to a
nudge.

### Set up the workspace

- Give the agent its **own workspace** to work in, so it doesn't collide with
  other parallel work. Which directory that is follows the task: for writing
  code a fresh git worktree is usually best, for something like ad-hoc data
  analysis a plain folder is usually best. The directory you pass becomes the
  tool's working directory. Keep the paths short and easy to type. The workspace
  can be any project; the scripts assume nothing about its layout.
- Two files carry the collaboration. **Single writer each**, so there is no lock
  to take and no clobber:
  - the **task file** (default `tasks.md` in the workspace) — **you** append
    tasks / answers / interrupts; the agent only reads it.
  - the **work file** (default `work.md` in the workspace) — the **agent**
    writes; you only read it. It holds:
    - a `STATUS:` line it overwrites every turn —
      `WORKING` | `DONE` | `NEED_INPUT` | `HANDOFF`
    - a running log of what it did, the decisions it made, and blockers
    - a `## Handoff` section it fills in on request (see *handoff* below)
- Putting these inside a worktree repo is fine — have the agent gitignore them or
  just skip committing the two.

### Bootstrap the session

Use the spawn script — it creates the two files if absent, pre-trusts the
workspace and presets Claude Code's first-run dialogs so an unattended spawn
cannot park on them, launches the tool in a persistent shell session with a
task-adapted TTL, and sends the contract message. Before launching Codex, read its
[`canonical-owner reference`](reference/canonical_codex_owner.md). Run from the
skill's directory:

```bash
# Claude Code
.venv/bin/python reference/spawn_claude.py <workspace-dir>

# OpenAI Codex
.venv/bin/python reference/spawn_codex.py <workspace-dir>

# Relocate either file (absolute, or relative to the workspace)
.venv/bin/python reference/spawn_claude.py <workspace-dir> --work-file notes/progress.md
```

On success it prints one `key=value` per line:

```
session_id=<id>
tasks_file=<absolute path>
work_file=<absolute path>
```

Codex's additional owner and state fields are defined in its reference.

**Take `work_file` from that output when you set up the watcher** — do not
rebuild the path from the default filename, or a relocated work file silently
leaves the watcher polling a file nobody writes. Interact via
`ava.shell.sessions.send` / `ava.shell.sessions.send_keys` / `ava.shell.sessions.capture` / `ava.shell.sessions.kill`.

For unusual manual setup, follow the scripts and the canonical-owner reference.

The contract (`reference/collaboration_protocol.md`) is addressed to the coding
agent and self-contained: the two files, the `STATUS:` values, the log
discipline, and the CHECKPOINT → handoff procedure. It refers to the two files
by role, not by name — the spawn script's launch message supplies their real
absolute paths.

### Startup check and supervision (mandatory)

The spawn script handles startup waiting and contract delivery automatically:
after launching the tool it polls ``ava.shell.sessions.capture`` until the
tool has rendered its UI, then sends the collaboration-contract message.
(``ava.shell.sessions.send`` splits text and Enter into separate calls
internally, avoiding the Enter-before-text race.)

**After either spawn script returns**, ``capture`` the screen to confirm the
agent received the contract message and is responsive.

For Claude, if the agent is alive, you MUST immediately launch the watcher
before doing anything else. Codex starts its canonical supervisor before its
PTY, so do not launch a second watcher for it.

If the agent is stuck on a permissions prompt or an error, handle it now —
otherwise the watcher will not see the blocker because it reads the work file,
not the screen.

For ``claude``, expect to see the agent reading the collaboration contract or
its task file. For ``codex``, expect to see it past the trust check and
beginning to plan or work.

### Supervise without watching the screen

**Every persistent coding session must have supervision.** Codex gets it
automatically from `spawn_codex.py`. For Claude, launch the reference watcher
(`reference/watch_work.py`) that polls the work file's `STATUS:` line and only
wakes you when there is something to do. Delivery retries across a gateway /
agent restart window, and if every attempt fails the watcher exits 2, so the
loss surfaces in its exit notice.

> **Use the reference watcher, don't write your own STATUS parser.** The
> reference (`reference/watch_work.py`) uses a regex (`^STATUS:\s*(\w+)`) to
> correctly extract only the value part. A common bug is writing
> `line.strip()` which gives the whole line `"STATUS: DONE"` instead of just
> `"DONE"`, causing the watcher to never fire. If you must write an ad-hoc
> watcher, use `line.split(':', 1)[1].strip()` or `re.findall(...)`. Also, never
> use bare `except: pass` — print errors to stderr so they appear in session
> capture when debugging.

- `DONE` → the Codex supervisor closes that generation; read the log and judge it.
- `NEED_INPUT` → read the question, write the answer to the task file, nudge.
- `HANDOFF` → the Codex supervisor closes the old generation; see *handoff* below.
- `WORKING` (or missing `STATUS:` line) — the agent is still going. The watcher
  has two safeguards so you are not left waiting indefinitely:

  **Stall detection** (`STALL_SECONDS`, default 600 s / 10 min): if the file
  hasn't changed at all while STATUS is WORKING (or missing), the agent may be
  crashed or hung — `capture` the screen yourself (you hold the session id) to
  check.

  **Heartbeat** (`HEARTBEAT_SECONDS`, default 480 s / 8 min): wakes you
  periodically even when the file *is* changing — catches the case where the
  coding agent keeps writing to the work file but never updates STATUS away from
  WORKING, or forgets the STATUS line entirely. A false wake costs little; a
  silent stall costs an hour.

  **Claude Code with Opus 4.8 / Fable 5 + xhigh effort** may work for 10–15 minutes
  without touching the work file — the model does a long reasoning pass before writing
  any output. For those runs, raise `STALL_SECONDS` to 900 (15 minutes) and
  `HEARTBEAT_SECONDS` to 720 (12 minutes) to avoid false alerts.

  > Models available on this machine: Fable 5 (`--model fable`), Opus 4.8
  > (`--model opus`). Start every session with `--dangerously-skip-permissions`
  > (required, not optional — Claude Code will hang on permission prompts otherwise).

A **nudge** is just `ava.shell.sessions.send(id, "read <task-file> and continue")`
(use the `tasks_file=` path the spawn script printed).
Interrupt with `send_keys(id, "C-c")`. That is all `send` / `send_keys` are for
now — not driving a menu. Long text (over roughly 1K characters) sent into a TUI
has its own rule — bracketed paste or a short pointer: [Delivery safety](reference/delivery_safety.md).

`capture` is for the rare stall check and for reading
the context indicator (below), not for routine progress.

The watcher's `timeout` parameter (set when you call `ava.watcher.launch`)
force-stops the watcher at the deadline and sends a best-effort timed-out wake
— if the poll loop is stuck the wake usually arrives, but it is not guaranteed
(the notifier is capped at 3 seconds). Set `timeout` comfortably above the larger of `STALL_SECONDS` and
`HEARTBEAT_SECONDS` — typically the task's expected duration (e.g. `"2h"`).

**Session naming convention:** always name sessions so their type is clear at
a glance in `ava.shell.sessions.list()`:

| Kind | Pattern | Example |
|------|---------|---------|
| Claude Code shell session | `claude-<dir>` | `claude-fix-auth-bug` |
| Codex shell session | `codex-<dir>` | `codex-refactor-db` |
| Watcher session | `watcher-<tool>-<dir>` | `watcher-claude-fix-auth-bug` |

Pass the watcher name to `ava.watcher.launch(name="watcher-claude-<dir>", ...)`.
The spawn scripts already set the shell session name; you only set the watcher name.

### Context limits and handoff

Neither tool reliably tells the **model** how full its context is — the figure
lives in the harness UI, not in the model's own view. So do not trust the agent
to notice; **you** read it and decide when to act. The reliable read is an
on-demand command, not the footer — the footer's context field is configurable
and off by default in some builds:

- `codex`: send `/status`; it prints `Context window: NN% left (X used / Y)`. Parse
  the percentage (see `reference/context_probe.py`). A `/statusline`-configured
  footer may also show `NN% context left`, but don't count on it.
- `claude`: `/context` prints a usage grid (no single number). A custom statusline
  may surface a `ctx:NN%` figure (percent *used*) you can scrape; otherwise lean
  on automatic compaction and proactive `/compact`, and fall back to a coarse
  proxy (turns elapsed, work-file size) to decide a handoff.

Two levers:

- **In-place compaction** — `send_keys(id, "/compact", "Enter")` in the live
  session. Cheap, keeps the session. (`/compact` is an interactive command; it
  does nothing sent to a headless `-p` run.)
- **Full handoff** (clean context) — append a `CHECKPOINT` line to the task file. The
  agent writes its `## Handoff` and sets `STATUS: HANDOFF`; you then `kill` the
  Claude session yourself. For Codex, follow the canonical-owner reference.

## Mode B — takeover (impersonation)

The tool **replaces you**: it talks to the human through your normal Ava chat
and calls Ava capabilities under your identity while your execution is paused.
A takeover is **file-less and supervisor-less** — nothing from Mode A applies:

- **The briefing is inline.** `--brief` inlines it verbatim into the launch
  message. There is no task file to read, no work file to write, and nothing
  watches a file.
- **Run it from your own workspace.** In most takeovers your own workspace is
  the best working directory: spawn the takeover under it and pass that
  directory as the spawn workspace argument, so the workspace is directly the
  impersonator's working directory. Other locations are not forbidden — this is
  the recommended default, not a requirement.
- **Start — the process interrupts you.** The launch call returns; when the
  takeover activates, the platform saves your checkpoint and your execution
  pauses. You and the replacement never run at the same time, so there is no
  lockstep to maintain and no supervisor to start.
- **End — one message resumes you.** When the takeover releases, a system note
  resumes you carrying its summary and the path of the handoff JSON
  (`impersonation/<session_id>.json`). Read that file before acting on pending
  human input; it retains all messages, including unACKed ones.

Launch it from your own execution context, briefing inline:

```bash
.venv/bin/python reference/spawn_codex.py <workspace-dir> --impersonate-self \
  --impersonation-name 'Fix login' --brief '<the full briefing text>'
```

`--brief` is required in this mode; `--tasks-file`/`--work-file` are refused —
a takeover reads no files. The workspace must not carry a live canonical
generation (`--cancel-generation <generation>` first). The takeover generation
is recorded without files or supervisor — its coding session alone is its
liveness signal — so do not start `watch_work.py` or any other file watcher
for it. The same command works with `spawn_claude.py` — its briefing starts
the executor's own Claude Monitor relay.

The full procedure is [Let the coding agent take over your identity](reference/impersonate_self.md);
the takeover process's own operating manual is the `impersonator-guide` skill.

## CLI reference

Tool versions, models and flags drift — every claim still starts with
`claude --help` / `codex exec --help` on the actual machine. The per-tool
references carry the launch variants, auth traps and headless notes:

- [Claude Code (`claude`)](reference/claude_code.md)
- [OpenAI Codex (`codex`)](reference/codex.md)
