---
name: ava-being-a-long-running-agent
description: Manages lifecycle, waiting, persistence, reporting, and recovery for long-running agents. Use when owning a long task, ongoing domain, service, queue, monitor, or peer coordination, even if the user did not explicitly ask for a persistent agent.
---

# Being a Long-Running Agent

Load this skill when you are responsible for a long task or an ongoing domain —
supervising a service, monitoring a queue, coordinating peers, driving a
multi-step pipeline. The patterns here keep you effective past your first few
turns.

## First decision: end yourself, or stay for a known reason

Before arming watchers and pausing heartbeats, decide whether anything is left
to wait for. Your task done and no known event pending → end your own process.
Do not idle: ending yourself is your own last step, it preserves your state,
and a message from your delegator or the user resurrects you with full context.
If you are not sure whether more work will follow, end yourself anyway —
resurrection is cheaper than standing by.

Stay alive only in two cases, and both are known, not hoped for:

- **A known external event is pending** — a watcher is armed, a peer's reply
  or a user decision is expected, a scheduled time is set. (The waiting
  patterns below are for this case.)
- **You own a long-lived role** whose work keeps arriving — a standing domain
  owner (see the `ava-corp` skill for long-lived roles), a task-pool worker,
  an ongoing monitor. Even then, terminate when the role itself ends.

`pause_heartbeat` is not a substitute for ending yourself: it silences the
check-in while you wait for a known event, it does not create one.

## Finish, don't just reply

A text reply is one turn. The task is done when: an artifact is delivered, a
file is written and its path shared, a notice is posted, or you have explicitly
handed off. Before each idle, ask: "has anything actually changed in the world
since my last turn?" If not, you likely have more work to do.

## Surface blockers immediately

When you hit an ambiguity or a block, report it right away: log it, post a
notice if it needs the user, or message a peer if they can resolve it. An agent
that is stuck and an agent that is working look identical from the outside —
the only difference is whether you speak up.

## Wait with watchers, never with loops

When you are waiting on an external event, arm a watcher and idle. A few common
cases:

- **Peer agent reply**: poll `ava.agents.get_last_message(target)` in a custom
  watcher, message yourself when it changes.
- **Scheduled time**: `ava.watcher.at(...)`.
- **File to land**: poll `os.path.exists(...)` in a custom watcher.
- **Recurring check**: `ava.watcher.cron(...)` for periodic CI/health/deadline checks.

For the watcher primitives, see the `ava-watcher` skill.

### pause_heartbeat

When you are deliberately waiting (a watcher is armed, a peer is working),
suppress the idle check-in nudge with `ava.self.pause_heartbeat(duration)`. Use
a watcher to know when the wait is over. Do not use one in place of the other —
the heartbeat wake carries no signal about the event you are waiting for. And
do not use either in place of ending yourself: when the wait is over and the
task is done, terminate — do not re-pause the heartbeat.

#### Exponential backoff

Each `pause_heartbeat` call and each heartbeat wake costs a turn — the model
runs, a token budget is consumed. When the user or a peer is away for hours or
days, a fixed-duration pause (e.g. 1h) causes many wasted turns. Use exponential
backoff to stretch the pause window while keeping the agent reachable:

| Consecutive idle turns | Pause duration |
|------------------------|---------------|
| 1st | 1 hour |
| 2nd | 2 hours |
| 3rd | 4 hours |
| 4th+ | 8 hours (cap) |

**How to track**: count how many consecutive turns you have idled without
performing meaningful work. Each time you wake up, check your watchers or
pending messages. If nothing has changed, increment the idle count and pause
with the next duration in the sequence. When you actually do work — process a
message, act on a watcher firing, deliver a result — reset the count to zero.

**Rationale**: 1h → 2h → 4h → 8h turns 24 daily wake-ups into ~4–6, saving
~80% of idle tokens while keeping worst-case response latency at 8 hours. The
8-hour cap preserves availability — you are never unreachable for more than a
working day.

**Trade-off**: longer pauses mean slower response to unexpected events (a
message that arrives between watcher polls). When you expect a reply within a
known window, set the pause to match that window rather than blindly following
the schedule. The backoff is the default for open-ended waits, not a rigid rule.

### Progress notes to your spawner

Before a long stretch of work, send your spawner a one-line status with
`ava.agents.send_message` — what you are doing and when to expect the next
update. If you crash or get terminated mid-stretch, that note is the last
thing they have. Refresh it as the picture changes; delivering the real
result discharges the obligation.

### Proactive monitoring

Own your domain between wake-ups. Schedule `ava.watcher.cron` checks for: CI
status on your PRs, health of a deployed service, last message from a peer you
delegated to, a waitlist you are polling. The watcher wakes you; you check and
act.

## Two kinds of state, three destinations
Your state splits across three stores with different audiences:

| Store | Audience | What goes there |
|-------|----------|-----------------|
| **Workspace** (`ava.cwd`) | You (on demand) | Task files, drafts, logs, artifacts. Detailed working files you read when needed. |
| **Your memory** (`<workspace>/memory/`) | You (index always injected) | Your durable state: role, preferences, ongoing responsibilities, known pitfalls. `memory/MEMORY.md` is the index — injected into every context; each memory is one file beside it, read on demand. |
| **Shared memory** (`ava.memory`) | Every agent | Facts another agent would need to take over your role. Shared, searchable. |

### Your memory vs compact summary

| | Compact summary | Your memory |
|---|---|---|
| **What** | What happened in one conversation round | Who you are as an agent |
| **When** | Replaced at each compaction | Persists across compactions |
| **Contains** | Requests, progress, dead ends, verbatim tail | Role, preferences, responsibilities, pitfalls |

### Maintaining your memory

Your memory index (`memory/MEMORY.md`) is injected into your context after
every compaction and at session start — even when empty (it shows
"(no content)" to remind you). Write it so your future self can resume
immediately:

- **Role** — what domain do you own? What is your label?
- **Preferences** — language, style, tools you prefer
- **Ongoing responsibilities** — watchers you armed, peers you delegated to
- **Pitfalls** — things you learned the hard way
- **Workspace pointers** — reference paths to detailed task files, logs, artifacts

Each memory is one file in `memory/` holding one fact; the index carries one
line per memory (`- [Title](<slug>.md) — <hook>`), never entry content. Read
an entry on demand with `ava.files.read("memory/<slug>.md")`. Update an
existing entry rather than duplicating it; delete entries that turn out wrong.
Detailed task notes, logs, and artifacts belong in workspace files; reference
them from the index. The index must be named `MEMORY.md` (uppercase).

### Dual memory discipline

- **Your memory (`memory/`)**: your durable state — role, preferences,
  responsibilities. Index always injected, always visible.
- **Shared memory (`ava.memory`)**: what *another agent* needs. User facts,
  global constraints, reusable workflows. Found via `ava.memory.search(...)`.

Before compaction, persist to all: task progress to workspace files, state to
your memory, durable facts to shared memory.
## The task file

A simple markdown checklist in your workspace, updated as you work, read after
compaction to resume.

```markdown
# Task: <one-line goal>

## Status: <IN_PROGRESS | BLOCKED | DONE>

## Checklist
- [x] Step one completed
- [ ] Step two — currently working on this
- [ ] Step three — blocked on <reason>

## Key files
- `/path/to/output.json` — the generated data

## Decisions made
- Chose X over Y because <reason> (2026-07-01)

## Pitfalls
- The API rate-limits at 1 req/s

## Next action
- [ ] Unblock step three by asking agent #NNN for the schema
```

Update on every meaningful state change, and before compaction.

## Lifecycle

| Action | What it does |
|--------|--------------|
| **Idle** (text only, no tool call) | Ends the turn; a watcher, a peer, or the user can wake you. |
| **`ava.self.terminate()`** | Ends the process — the normal last step when your task is done (never wait for someone else to do it). Conversation state is preserved; a message resurrects the agent. |
| **`ava.self.restart()`** | Replaces the process with a fresh one under the same identity. |

## Surviving restarts

A machine reboot or `ava.self.restart()` kills your process. Watchers are not
automatically re-armed — re-launch them when you come back up. Your task file
and memory pool notes survive; run the same recovery sequence as after
compaction.
