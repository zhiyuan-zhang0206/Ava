# Task maintenance as a gateway daemon, not an agent-launched watcher

## Context

The task registry (`agent_tasks`, exposed as `ava.tasks.*`) grew two adjacent
capabilities that were never actually wired to run:

- `remind_after` / `last_reminded_at` columns and a reminder query, meant to nudge
  the owner of an in-progress task that has gone quiet.
- No stale detection at all: a task owned by an agent that later terminated stayed
  `in_progress` with a dead owner forever.

An audit of the live registry showed dozens of `in_progress` tasks owned by
terminated agents, every task with `remind_after` and `last_reminded_at` NULL.
The reminder logic existed only as a *reference script* that an agent had to
notice, copy, and launch. Nothing did, so nothing ran.

## Decision

Run task maintenance as a **gateway-owned background daemon**
(`services/task_maintenance/daemon.py`), registered in `build_services()` like
`heartbeat`. One per cluster, started by `ava start`, health-checked and respawned
by the gateway watchdog. It does two passes:

- **Nudge** every `AVA_TASK_MAINTENANCE_INTERVAL_SECONDS` (default 5 min): find
  in-progress tasks whose owner has not touched them within their `remind_interval`
  (per-task, default 30 min) and INSERT a chat nudge to the owner. Terminated
  owners are included — the inbound-insert trigger auto-resurrects them. A backoff
  (`AVA_TASK_NUDGE_BACKOFF_SECONDS`, default 1h) prevents duplicate nudges within the
  same overdue window.
- **Escalate** when `nudge_count` reaches `AVA_TASK_ESCALATE_N` (default 3):
  notify the parent task's owner that the current owner is unresponsive.

Two supporting changes: new tasks default `remind_interval` to 30 minutes
(`create()` signature default; pass `None` to opt out), and any `update()` call
resets `last_nudged_at` and `nudge_count` (the reminder window starts fresh). The
reference `task_reminder.py` is deleted — the daemon subsumes it.

## Design evolution

The original design (PR #397 draft) included a **stale sweep** that cancelled
tasks untouched for 7 days and released tasks owned by terminated agents. This
was rejected during review in favour of an agent-driven model:

- The system only **nudges** — it never changes task state based on wall-clock
  time. Cancellation is an agent action, not a system action.
- Terminated owners are nudged (auto-resurrected) rather than having their tasks
  released. The agent may have finished the work but not updated the status.
- No `hold()` API — any `update()` naturally resets the nudge window, so an
  actively-working agent needs no extra calls.

## Alternatives rejected

- **Stale sweep with time-based cancellation** (original PR draft): cancelling
  tasks after 7 days conflates "abandoned" with "takes 8 days." The system
  cannot distinguish these.
- **Literally "auto-launch the reference watcher on startup"**: `ava.watcher.*`
  is agent-scoped and dies with the agent. Structurally cannot be a cluster-wide
  service.
- **Fold into heartbeat daemon**: rejected to keep independent disable/enable
  and separate concerns.
- **`hold()` API** (intermediate design): rejected as redundant — `update()`
  already serves the same purpose of signalling "still working."

## Consequences

- Reminders now happen with zero agent involvement; the fleet skill documents
  them as automatic rather than as a script to launch.
- One gateway service (port 8108, roster row, healthcheck). Gateway-capability
  only and cluster-wide, so a split deployment runs exactly one.
- **Escalation to parent owner** is the only automatic action beyond nudging —
  the delegator is notified when the delegatee is unresponsive.
- History is preserved: every transition is an UPDATE, never a DELETE.
