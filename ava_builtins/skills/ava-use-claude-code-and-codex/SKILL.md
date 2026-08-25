---
name: ava-use-claude-code-and-codex
description: Drives Claude Code or Codex CLI as supervised long-running coding agents. Use when outsourcing multi-step implementation or review, choosing between the two CLIs, resuming a coding session, or monitoring delegated coding work.
---

# Use Claude Code and Codex

Both `claude` (Anthropic) and `codex` (OpenAI) are coding-agent CLIs you can hand
a task to and let plan + execute. Treat either as "another agent". This skill
covers when to use each, the file-driven pattern
for supervising a long task. For the session primitives themselves see
`ava.shell` (`run` for one-shot; `sessions` — `new` / `send` / `send_keys` /
`capture` / `kill` — for a persistent one) and `ava.watcher`; don't re-derive
those here.

> **Flags and models drift between releases.** Everything below is a snapshot,
> not a contract. Confirm with `claude --help` / `codex exec --help` and `claude
> --version` / `codex --version` on the actual machine before relying on a flag —
> and note a machine may have only one of the two installed.

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

## Two ways to run it

**Persistent session is the default.** Start it in `ava.shell.sessions` and steer it across
many turns — the collaboration pattern below builds on this. Headless one-shot (`claude -p`,
`codex exec`) exists but is rarely needed; avoid it unless the task is truly self-contained and
needs no supervision.

## File-driven collaboration (the pattern for long tasks)

Don't supervise by reading the agent's screen. The screen is a redraw you have to
parse and it vanishes on restart; a file the agent writes is structured, durable,
and diffable. Make a shared work folder the interface and demote keystrokes to a
nudge.

### Set up the workspace

- Give the agent its **own workspace** — a fresh git worktree, or just a
  dedicated folder — so it doesn't collide with other parallel work. Keep the
  paths short and easy to type. The workspace can be any project; the scripts
  assume nothing about its layout.
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

Use the spawn script — it pre-trusts the directory, creates the two files if
absent, launches the tool in a persistent shell session via
`ava.shell.sessions.new("claude-<dirname>")` or `codex-<dirname>`, and sends the
collaboration-contract message. Run it from the skill's reference directory:

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

**Take `work_file` from that output when you set up the watcher** — do not
rebuild the path from the default filename, or a relocated work file silently
leaves the watcher polling a file nobody writes. Interact via
`ava.shell.sessions.send` / `ava.shell.sessions.send_keys` / `ava.shell.sessions.capture` / `ava.shell.sessions.kill`.

If you need to do the steps by hand (unusual), the scripts are the reference:
they set `hasTrustDialogAccepted` in `~/.claude.json` (claude) or append a
`[projects."<abs>"]` table with `trust_level = "trusted"` to
`~/.codex/config.toml` (codex), then launch with the flags listed in the
per-tool sections below.

The contract (`reference/collaboration_protocol.md`) is addressed to the coding
agent and self-contained: the two files, the `STATUS:` values, the log
discipline, and the CHECKPOINT → handoff procedure. It refers to the two files
by role, not by name — the spawn script's launch message supplies their real
absolute paths.

### Startup check → launch watcher (mandatory)

The spawn script handles startup waiting and contract delivery automatically:
after launching the tool it polls ``ava.shell.sessions.capture`` until the
tool has rendered its UI, then sends the collaboration-contract message.
(``ava.shell.sessions.send`` splits text and Enter into separate calls
internally, avoiding the Enter-before-text race.)

**After the spawn script returns**, ``capture`` the screen to confirm the
agent received the contract message and is responsive.

**If the agent is alive: you MUST immediately launch the watcher before doing
anything else — skipping the watcher is a bug.** Without a watcher the coding
agent runs unsupervised: you are never woken when it finishes, needs input, or
stalls. See the next section for the watcher code and setup.

If the agent is stuck on a permissions prompt or an error, handle it now —
otherwise the watcher will not see the blocker because it reads the work file,
not the screen.

For ``claude``, expect to see the agent reading the collaboration contract or
its task file. For ``codex``, expect to see it past the trust check and
beginning to plan or work.

### Supervise without watching the screen

**You must launch a watcher for every persistent coding session.** Use the reference watcher (`reference/watch_work.py`) that polls the work file's `STATUS:`
line and only wakes you when there is something to do — so you don't burn turns
polling.

> **Use the reference watcher, don't write your own STATUS parser.** The
> reference (`reference/watch_work.py`) uses a regex (`^STATUS:\s*(\w+)`) to
> correctly extract only the value part. A common bug is writing
> `line.strip()` which gives the whole line `"STATUS: DONE"` instead of just
> `"DONE"`, causing the watcher to never fire. If you must write an ad-hoc
> watcher, use `line.split(':', 1)[1].strip()` or `re.findall(...)`. Also, never
> use bare `except: pass` — print errors to stderr so they appear in session
> capture when debugging.

- `DONE` → read the log, judge it, append the next task to the task file, nudge.
- `NEED_INPUT` → read the question, write the answer to the task file, nudge.
- `HANDOFF` → see *handoff* below.
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
now — not driving a menu.

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
  session and start a fresh one with the same bootstrap pointed at the **same
  folder**. The file is the memory, so the new session continues without losing
  the thread.

## Claude Code (`claude`)

For an interactive (persistent) session, use the spawn script instead of
launching by hand — it pre-trusts the directory and sends the contract message:

```bash
.venv/bin/python reference/spawn_claude.py <workspace-dir>
```

Manual launch (for full control):

```bash
cd /path/to/workspace && unset ANTHROPIC_API_KEY && claude --dangerously-skip-permissions   # interactive session (the persistent pattern)
```

Headless one-shot (`claude -p "<task>"` / `claude -p "<task>" --output-format json`) is
available for rare, self-contained tasks that need no supervision — avoid by default.

- **Required flags on this machine:** `--dangerously-skip-permissions`.
  Available models: Fable 5 (`--model fable`), Opus 4.8 (`--model opus`).
  Other flags (verify with
  `claude --help`): `--output-format
  json|stream-json` (only with `-p`), `--continue` / `--resume <session_id>`.
- In an interactive session, `/compact` (and silent auto-compaction near the
  limit) manage context; `/context` shows usage but as a grid, not a number.
- `--output-format json` returns per-turn token usage (input / output / cache)
  and the model's `contextWindow`; it does **not** report cumulative session
  usage — sum it yourself if you track headroom in headless mode.
- **Auth & billing trap:** `ANTHROPIC_API_KEY` must be **unset** before launching
  `claude`. When the env var is present alongside a paid Claude subscription,
  Claude Code defaults to API-key billing — incurring per-token charges instead
  of using the subscription. The spawn script already unsets it in the session.
  For manual launch, prefix the command with `unset ANTHROPIC_API_KEY &&`.
  (Agents calling Claude through Ava's model layer are unaffected — the key is
  picked up from the server config, not the agent's environment.)

## OpenAI Codex (`codex`)

For an interactive (persistent) session, use the spawn script instead of
launching by hand — it pre-trusts the directory and sends the contract message:

```bash
.venv/bin/python reference/spawn_codex.py <workspace-dir>
```

Manual launch (for full control):

```bash
codex --dangerously-bypass-approvals-and-sandbox -C <dir>   # interactive, hands-off
```

Headless one-shot (`codex exec "<task>" ...`) is available for rare, self-contained tasks
that need no supervision — avoid by default. `codex exec review --uncommitted` is the exception
for code review; `codex exec resume --last "<follow-up>"` continues a previous session.

- `codex exec` **always returns exit code 0**, even when the task failed — judge
  success from the output, not the return code.
- Useful flags (verify with `codex exec --help`): `-s/--sandbox`
  (`read-only` / `workspace-write` / `danger-full-access`), `-a/--ask-for-approval`
  (`on-request` / `never`), `--ephemeral`, `-C/--cd <dir>`, `--skip-git-repo-check`,
  `--json` (emit JSONL).
- For a **hands-off interactive** session use
  `--dangerously-bypass-approvals-and-sandbox` — the file-driven pattern can't
  answer approval prompts. `-s danger-full-access` alone is not enough: the
  default approval policy is `on-request`, so it still pauses to ask. Pair it with
  `-a never`, or use the single bypass flag. Intended for an already-sandboxed host.
- `codex exec --json` puts per-turn token usage on `turn.completed` events
  (`input_tokens` / `cached_input_tokens` / `output_tokens` /
  `reasoning_output_tokens`); it does **not** include a context-window percentage.
- In an interactive session, `/status` reports `Context window: NN% left
  (X used / Y)` and `/compact` (manual + automatic) manages context; the footer's
  context field is off by default in some builds.
- Codex expects a git repo; pass `--skip-git-repo-check` if it is not one.
- Auth: `OPENAI_API_KEY`, or `CODEX_ACCESS_TOKEN` for a ChatGPT-account login.
