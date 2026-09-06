---
type: doc
title: ava-fleet skill — Fleet Collaboration Master Outline
description: "Master outline skill for fleet collaboration: a one-shot orchestrator decomposes large goals, forks workers, is woken to gather and judge results; communication bisection—authorization/decisions reach the user directly from any depth, progress/conclusions roll up the delegation tree to the delegator for aggregation. Includes orchestrator / worker role manuals and a watch_idle.py reference script."
tags:
- fleet
- agent-instruction
- orchestration
---

# ava-fleet skill — Fleet Collaboration Master Outline

## What It Is

The master outline of the fleet collaboration methodology (`plugins/ava_fleet/skills/ava-fleet/SKILL.md`, loaded name `ava.skills.ava_fleet`, indexed in every agent's prompt like every other loaded skill). fleet = a graph of agents working toward a human goal; the human only controls **decomposition** (how the work is split) and **judgment** (the results that come back), never opening any agent's conversation. Agents are general-purpose—their division of labor comes from the spawn-time instructions, not from which skills they were handed: a worker's index already lists everything its machine has.

## Communication Bisection (#620 Revision)

The old tenet "any agent reaches the user directly, no relay chain" was over-extended—raw worker output flooded the user queue. The bisection corrects this:

- **Authorization / decisions** (things only the user can determine)—reach the **user directly** from any depth (user sovereignty preserved).
- **Progress / conclusions**—sent to one's own **delegator** (the agent that spawned / assigned the work, i.e., the parent task's owner) via `send_message` for digestion and aggregation, only later possibly entering the user's view.
- **Milestone-based messaging (user ruling 2026-09-03):** peer
  `send_message` traffic is milestone-based by default — real milestones,
  blockers, completion, or genuine requests. Routine progress pings and
  bare acknowledgments are noise (each message costs the recipient a
  turn). Task-assignment delivery stays full-text: a summary-plus-link
  redesign would cost an extra round-trip.
- No task, no delegator: deliver directly; do not fabricate a hierarchy just to forward.
- Each shared milestone has one reporter and one action owner named in the
  brief. Other participants report changes instead of relaying an unchanged
  result or copying it into the action owner's task log; non-owner task
  writes can notify that owner too.

## Task ↔ Fleet Interaction Rules

The task registry is the durable record of work; the spawn graph is the transient communication network. Fleet reaction rules when task state changes:

- **created** (with `parent`) — the parent task's owner implicitly oversees the creator; no automatic message.
- **done** → notify the delegator: task id + result location (file/PR/page) + a one-line summary, then end your own process (`ava.self.terminate()`) — the notification plus recorded results is the handoff, and a follow-up message from the delegator resurrects you with context intact; if the delegator is unknown (no owning task), record the completion in the task's results.
- **blocked** → there is no blocked status: stay `in_progress`, record the blocker in `content`, escalate to the delegator; exceptions requiring user decision reach `ava.ui.notify(require_response=True)` directly.
- **reassign** → `ava.tasks.update(owner=)` automatically notifies both old and new owners (see [[ava_builtins/plugins/ava_fleet/tasks/task_notification.ava.okf.md|Task Notification]]).
- **cancelled** → clean up active child tasks, notify the working agent.
- **remind_interval_seconds** (default 30min) overdue without update → daemon nags the owner; after 3 unanswered escalations: if there is a parent task owner, notify them; if no parent owner (top-level task whose parent is the ownerless system root), insert a require_response reminder into the human queue, hung on the stuck owner, grouped by task, inheriting its priority (#663).

## Autonomous Batch Work (Autonomy Dial Implementation)

The Autonomy axis of the Two Dials (who consumes evidence to make decisions) conventions during batch autonomous work:

- **One approval buys high Autonomy**: before batch autonomy, package the scope + task list (`ava.tasks.create()` items) + budget cap into a **single** `ava.ui.notify(require_response=True)`; the user's reply authorizes it, and **the original text is recorded** into the driving task's `description`. After approval, do not ask step-by-step; whether the user is online does not change behavior.
- **Safety boundary** (the authorization broadens what can be done, does not remove guardrails): only produce PRs without merging, do not send outward messages, do not touch prod, do not spend money beyond the authorization.
- **Budget soft enforcement**: the spawner uses `ava.watcher.launch/cron` to periodically run `reference/usage.py` (reporting per-agent + total spend from the unified `events` stream); near the cap → send a message urging workers to converge; over the cap → send a message ordering cleanup and stop (wrap up, update task statuses, self-terminate). **No automatic force-kill**—relies on task-maintenance overdue nags + `AVA_TASK_ESCALATE_N` (default 3) escalating to the parent task owner; whether to kill is up to the superior. The budget figure is written per-approval into the authorization text (example soft $3 / hard stop $3.5, not a constant).

## Reference Sub-documents

- `reference/orchestrator.md` / `reference/worker.md` — per-role deep-dive manuals: delegator semantics of the first prompt, delivery path (results = write a file + send the path, siblings only consume published artifacts), reporting discipline.
- `reference/watch_idle.py` — reference script for a goal-mode watcher (wake and judge when the goal is idle).
- `reference/usage.py` — budget table: aggregates `llm_usage` by `--agent-id`/`--since`/`--hours`, priced via `shared.lm.pricing.cost_usd`, outputs JSON (caliber mirrors `agent_inspect._agent_cost`).

## Key Dependencies

- [[ava_builtins/plugins/ava_fleet/skills/reduce-context-switch-for-human/reduce-context-switch-for-human.ava.okf.md|interruption discipline skill]] — the same bisection on the notification queue side
- [[ava_builtins/plugins/ava_fleet/notify.ava.okf.md|Notify]] / [[ava_builtins/plugins/ava_fleet/tasks/tasks.ava.okf.md|Tasks]] / [[ava_builtins/plugins/ava_fleet/spawn.ava.okf.md|Spawn]] / [[ava_builtins/plugins/ava_fleet/neighbors/neighbors.ava.okf.md|Neighbors]] — the SDK primitives orchestrated by the skill
