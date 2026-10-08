---
name: ava-being-a-long-running-agent
description: "Manages persistent Ava work and recovery. Use when a task spans turns, waits for events, or resumes after interruption."
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

Use a watcher or existing event delivery for a wait. Before choosing heartbeat
pauses, backoff, mechanical monitors, or peer wait coordination, read
[waiting](references/waiting.md). Wake only on actionable changes; stop owned
one-off monitors when done, and terminate when the finite task is complete.

## Usage budget reminders

For agent usage observations and USD cost reminders, read [usage reports](references/usage.md)
and use `scripts/agent_usage.py`. Select explicit IDs, a time window or lifetime,
and spawn/fork birth lineage; task records are not a spending ledger. Optional
polling sends a one-shot reminder to named peers and exits without termination.

Treat a reminder as a decision point. Preserve useful work and recovery notes,
converge, hand off, or seek a revised budget according to remaining work and
existing authority. Bound the observer lifetime and retain its session identity
if it needs cancellation or recovery. A reminder does not authorize more spending.

## Two kinds of state, three destinations

Keep detailed progress in workspace files, durable personal state in
memory, and shared facts in the pool. Read [durable state](references/durable-state.md)
when choosing destinations or preparing for compaction/recovery.

## The task file

Read the [task-file example](references/durable-state.md#the-task-file) when a
resumable checklist is useful. Update it on meaningful changes and before compaction.

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
