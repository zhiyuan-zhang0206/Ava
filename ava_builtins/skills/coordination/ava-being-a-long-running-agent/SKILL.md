---
name: ava-being-a-long-running-agent
description: Manages lifecycle, waiting, persistence, reporting, and recovery for long-running agents. Use when owning a long task, ongoing domain, service, queue, monitor, or peer coordination, even if the user did not explicitly ask for a persistent agent.
---

# Being a Long-Running Agent

Load this skill when you are responsible for a long task or an ongoing domain —
supervising a service, monitoring a queue, coordinating peers, driving a
multi-step pipeline. The patterns here keep you effective past your first few
turns.

## Scope and operating procedure

The core system prompt's **Efficient long-running operation** section owns the
lifecycle and cost principles, including for agents without fleet collaboration.
This skill supplies the procedures for waiting, monitoring, recovery, and durable
state. The fleet plugin owns agent-to-agent communication; use its contract when
coordinating peers rather than inventing a separate reporting cadence here.

Before setting up a wait, record the expected event, how it will reach you, the
required response time, and the monitor or schedule reference in your task file.
For an ongoing role, record its monitoring responsibilities and end condition in
your personal memory. Check existing monitors before creating one.

## Finish, don't just reply

A text reply is one turn. The task is done when: an artifact is delivered, a
file is written and its path shared, a notice is posted, or you have explicitly
handed off. Before each idle, ask: "has anything actually changed in the world
since my last turn?" If not, you likely have more work to do.

## Improve the recurring work you own

Long-lived ownership includes making repeated work easier. When setup,
validation, recovery or coordination keeps consuming effort, use the
`ava-workflow` skill's investment loop (section "Invest in future work")
to address the recurring cause. A long task alone does not justify new tooling
or infrastructure.

For a chosen investment, keep these details in the existing task file before
compaction, handoff or restart; use memory pointers for what future owners need:

- **Purpose and authority** — the larger human goal, supporting evidence,
  settled decisions and current constraints.
- **Investment** — the causal chain and evidence, current intervention and
  consumers, the next checkpoint and unresolved questions about the root cause.
- **Reuse** — the tool or artifact location, how to use it, verified results,
  remaining caller migrations or obsolete paths to delete, and the next action.

On resuming, read this record and check what has changed before repeating setup
or inventing another workaround. Reuse settled instructions; an inferred goal
does not expand authorization. Exercise the improvement on real consumers and
record what each checkpoint establishes. Continue following the recurring cause
through completed integration, even when it reaches a deeper architectural layer
or questions the project's purpose. A session boundary or passed checkpoint is
not a reason to reset the diagnosis or return to feature work with the cause
unresolved; explicit authority and resource limits still apply.

## Surface blockers immediately

When you hit an ambiguity or a block, report it right away: log it, post a
notice if it needs the user, or message a peer if they can resolve it. An agent
that is stuck and an agent that is working look identical from the outside —
the only difference is whether you speak up.

## Wait with watchers, never with loops

When you are waiting on an external event, arm a watcher and idle. A few common
cases:

- **Peer agent reply**: a message addressed to you wakes you — nothing to poll.
  `ava.agents.get_last_message(target)` is not a reply signal: it returns the
  peer's last AI *turn text*, `None` when that turn had no text (e.g. the peer
  answered by `send_message`, task #3656). Poll it only when the signal you
  await genuinely is turn text; for liveness use `ava.agents.get_status(target)`.
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

**Rationale**: this example reduces repeated idle check-ins. Choose the cap from
the required response time; it is not a guaranteed token saving or delivery bound.

**Trade-off**: polling intervals determine how soon a watcher detects a condition.
Heartbeat pauses suppress check-ins, not delivery of messages or watcher events.
Do not rely on a heartbeat wake as the signal for an awaited event; arrange its
own delivery and choose polling intervals to satisfy the response requirement.

### Monitoring without a model turn on every tick

Use `ava.watcher.cron` or a schedule when the recurring work itself needs model
judgment. For mechanical CI, file, queue, or health checks, use a custom background
watcher that checks the condition and sends a message only when you must act.
Load `ava-watcher` for implementation details. Set the interval and lifetime from
the response requirement, and reuse existing event delivery or a monitor when it
already covers the wait.

Compare the condition relevant to action, not raw readings: disk usage moving
within a healthy range is not a wake trigger; reaching the intervention threshold
is. Keep ordinary samples in a log. A wake message should name the condition,
the relevant evidence, and the durable record to resume from.

When the wait resolves or is cancelled, stop the owned monitor if it is no longer
needed. Keep recurring role monitors while that role remains active. On recovery,
inspect recorded monitor references and current status before replacing them.

### Coordinating a wait with peers

Use the fleet communication contract for milestones, blockers, commitments, and
handoffs. When another agent relies on your acceptance or timing, send that
commitment with the useful update; do not send a preliminary status solely because
you are about to work for a long stretch. Persist intermediate progress in the
task file so recovery does not depend on a sequence of messages.

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

Each compaction also dumps the raw pre-compact message history into your
workspace under `message-history/` (JSONL, one message per line) — grep it when
the summary misses a detail you need.

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

A machine reboot or `ava.self.restart()` replaces your process. Check the recorded
watcher and schedule references when you return; restore missing monitors that
are still needed, without duplicating ones that survived. Your task file
and memory pool notes survive; run the same recovery sequence as after
compaction.
