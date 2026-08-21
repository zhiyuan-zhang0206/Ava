---
name: ava-fleet
description: A disposable orchestrator decomposes a large goal, forks workers, gathers them by being woken, and judges the result; decisions reach the user directly, progress rolls up the delegation tree. Use when a goal is too big for one run, when you spawn workers to parallelize, or when spawned as a worker.
---

# Working in a Fleet

A **fleet** is a graph of agents working toward a human's goals, supervised from one **fleet view** — a live picture of who is responsible for what, and what each is doing right now. The human steers *decomposition* (how the work is split) and judges *results* (what came back), without opening any agent's conversation. You are one node in that graph: you may direct other agents and be directed by them at any moment, and the relationships that matter are who is talking to whom right now, not any fixed rank.

Agents are general-purpose. Every worker already indexes every skill on its machine, so division of labor comes from what each agent is asked to do — name the skill you expect it to use in the spawn prompt. (`config_overlay` can still set `skills_to_inject_into_system_prompt`, but under a universal index that only *narrows* a worker's view; it grants nothing.)

## Before taking on work: observe, ask, then spawn

New work arrives at an agent all the time — a task assigned, a message, a noticed gap. Before grabbing it yourself, check whether someone already owns this slice of the fleet. Three steps, cheapest first:

1. **Observe** — look at the agents around you and their labels. A label names a domain ("health steward", "memory maintenance"); if one already names this work, that agent is the owner. Hand it over instead of taking over.
2. **Ask** — if no label clearly covers the work, message the closest peers and ask who owns it, or who has the context for it. Labels are one-line summaries; the real division of labor lives in what agents know. A quick question beats a duplicate effort.
3. **Spawn** — only when observing and asking place it nowhere: spawn a worker for it (name the skill it should use in the spawn prompt). Spawning is the fallback, not the default — every spawn you avoid is context and memory you did not duplicate.

If you were spawned for a specific sub-task, skip the check and finish that sub-task — do not expand into adjacent domains.

## Task ↔ Fleet Interactions

The task registry is the durable record of work; the spawn graph is the ephemeral web of who is talking to whom. When a task changes state, the fleet reacts — here are the rules that connect the two.

### Task Created

When `ava.tasks.create()` is called with a `parent`, the parent task's delegator (its owner) is implicitly overseeing the creator. No automatic message is sent — the parent-task owner discovers the new subtask by listing tasks with `parent=...`. When the task is created as part of a fleet delegation (an orchestrator spawns a worker and tells it to self-assign), the orchestrator should already be watching via the goal pattern.

### Task Status Done → Notify Delegator

When a worker sets a task to **done**, it must notify the agent that delegated the task — the orchestrator / delegator that spawned it or assigned it the work. Use `ava.agents.send_message(delegator_id, ...)` with:
- The task id and title
- Where the result lives (file path, PR link, served page)
- A one-line summary of the outcome

The delegator receives this and can verify, merge, or reduce without polling. If the delegator is unknown (an unowned task finished by a claimer), record the completion in the task's results — no one is waiting.

Then **end your own process** (`ava.self.terminate()`). The notification plus the recorded task results are the handoff — do not idle waiting to be terminated by the delegator. If a follow-up is needed, the delegator messages you, and that message resurrects you with your context intact.

### Task Blocked → Escalate to Delegator

A worker that hits a blocker does **not** create a "blocked" status (there is no such state). Instead:
1. Leave the task `in_progress`
2. Update `content` with the blocker description and what is needed to unblock
3. Send the delegator a message describing the block and what decision or resource is needed — this is an escalation, not a failure

If the delegator cannot resolve the block either, they escalate further up the task tree. When a block needs the user's decision (irreversible action, spending, outward-facing), use `ava.ui.notify(require_response=True)` — do not route user decisions through the agent chain.

### Task Reassign → Notify Old and New Owner

When `ava.tasks.update(id, owner=...)` moves a task:
- The **new owner** receives: "Task #N 'Title' is now assigned to you (by agent #X)." It should read the task's `description` and `results`, then begin or decline.
- The **old owner** receives: "Task #N 'Title' you owned is no longer assigned to you."
- The **delegator** (parent task's owner) is **not** automatically notified — they see the owner change when they next check. If the reassignment is significant (a hand-off to a specialist), the old owner should message the delegator separately.

This is handled automatically by `ava.tasks.update()` — the caller only needs to pass `owner=`. The function skips terminated agents and never notifies the caller about themselves.

### Task Cancelled → Clean Up Dependencies

When a task is cancelled:
1. The canceller should check for open subtasks (`ava.tasks.list(parent=task_id, status="open")`) and cancel or reassign them — orphaned subtasks with a cancelled parent are ambiguous.
2. Any agent actively working on a now-cancelled task should be messaged so it can stop or pivot.

### Reminder Mechanism

A task's `remind_interval_seconds` (seconds) triggers a reminder to its owner if the task is not updated within that window — a chat message telling the owner the task has gone quiet. New tasks default to 30 minutes (`ava.tasks.create(...)` sets `remind_interval_seconds=1800` unless you pass a different value; `None` also means the default — reminders cannot be turned off). Any `ava.tasks.update()` resets the reminder counters. The daemon also escalates to the parent task's owner after 3 unanswered reminders. Reminders are useful for:
- Long-running tasks where the orchestrator wants a periodic pulse
- Tasks assigned to agents that might stall silently
- Health checks on delegation chains

The reminder is sent at most once per overdue window — updating the task resets the window, so an agent that reports progress regularly never gets pinged.

Reminders fire automatically — the cluster runs a task-maintenance daemon that reminds overdue task owners. You do not launch anything. After 3 unanswered reminders the daemon escalates to the parent task's owner.
## Two Roles

Every agent in a fleet is either an **orchestrator** (delegating work to others) or a **worker** (carrying out a delegated mission). These are roles you slip into at runtime, not fixed identities — the agent you spawned this morning may spawn its own workers this afternoon.

| Role | What you do | Read |
|---|---|---|
| **Orchestrator** | Decompose a goal, spawn workers with the right skills, supervise them, gather and judge results | [reference/orchestrator.md](reference/orchestrator.md) |
| **Worker** | Own a mission end to end, report results to your delegator, reach the user for their decisions | [reference/worker.md](reference/worker.md) |

Both roles share the same communication primitives: `send_message` for peer-to-peer, `ava.ui.notify` for the user, and task updates for the durable record. Reporting responsibility is split in two: anything needing the user's **authorization or decision** goes to the user directly, from any agent — that authority is never relayed. **Progress and conclusions** roll up to the delegator (the parent task's owner), who aggregates before the user sees anything — raw per-worker results do not land in the user's queue. An agent with no delegator delivers directly. How often that roll-up reports up — its **cadence** — is agreed at delegation time from the nature of the work, not fixed by the framework. Interruption discipline while the user is away: the `reduce-context-switch-for-human` skill.

## Two Dials: Effort and Autonomy

Every delegation is set along two independent axes — per task, and (because decomposition is recursive) per reduce point, not once globally.

- **Effort** — how much capability you spend to get a node right, in three fungible currencies: a **stronger model**, a **finer split** (decomposing further — deciding where to cut is itself intelligence), and **harder verification** (a glance, then one adversarial evaluator, then several distinct-perspective evaluators). They trade off: a hard sub-task can buy quality with one strong-model run *or* several cheap runs cross-checked by an adversarial judge.
- **Autonomy** — who consumes the evidence and makes the call. Low: a human decides at every reduce point. High: you decide everything and the human sees only the final result. Independent of Effort — you can pile up a deep multi-lens evaluation and *still* hand the decision to a human (high Effort, low Autonomy), or barely evaluate and run on your own (low Effort, high Autonomy).

A small parallel delegation you eyeball yourself is just **low Effort + low Autonomy**; a background pipeline that decomposes, evaluates adversarially, and converges on its own is **high Effort + high Autonomy**. Same machinery, two dial settings — not different modes.

**Setting Autonomy is not a config flag — it is a message.** When you want the user to make a reduce-point call, you do not write a new state field; you `ava.ui.notify(..., require_response=True)`; their answer arrives as a message. The user "turning the dial down" on a node is simply you waiting for their decision. No shared mutable state, no schema — the existing message paths *are* the dial.

**High Autonomy is bought with one upfront approval, not many.** Before a batch of autonomous work, package the whole ask into a single `ava.ui.notify(require_response=True)`: the scope you intend to cover, the task list you will work (the `ava.tasks.create()` items), and the budget ceiling in dollars. The user's reply is the authorization — record it verbatim in the driving task's `description`, so any agent that picks the work up sees exactly what was granted. Once granted you run: no step-by-step check-ins, and whether the user is online or away changes nothing about how you proceed.

## Autonomy's boundaries

Authorization widens what you may do on your own; it does not remove the rails. Inside any autonomous run, whatever the dial:

- **Produce PRs, never merge them** — a human, or a reviewer explicitly delegated the merge, merges.
- **No outward messages** — nothing sent to real people or external services on your own authority.
- **Don't touch prod** — no deploys, no prod data-plane writes, no rollout.
- **Spend only what was authorized** — the budget in the approval is a ceiling, not a target.

The budget ceiling is enforced softly: a watcher meters, a human decides the hard stop. The spawner points a watcher (`ava.watcher.launch` / `ava.watcher.cron`) at `reference/usage.py`, which reports per-agent and total spend from the LGTM read side (the durable cost ledger + Loki — the PG `events` table is a frozen archive):

- **Approaching the ceiling** → message the worker to converge: finish the current thread, stop opening new ones.
- **Over the ceiling** → message the worker to clean up and stop: finish the in-flight unit, update its task status.

There is no automatic force-kill. A worker that ignores the message is caught by the same self-healing loop as any stalled worker: its overdue task draws a task-maintenance reminder, and if reminders go unanswered the escalation chain (after `AVA_TASK_ESCALATE_N` reminders, default 3, the parent task's owner is notified) hands the kill-or-not decision to whoever is above it. Budget numbers are per-approval, written into the authorization text (e.g. soft ceiling $3, hard stop $3.5 — an example, not a constant).

## Reaching the User vs Another Agent

The **user** is the only one who can authorize what no agent may decide alone — an irreversible or outward-facing action, spending real money, sending something to a real person. This authority is not delegated down the spawn graph: the user is a single queue, reachable directly from any agent.

- **Another agent can decide it** → `send_message` them — you are waiting for their answer.
- **Only the user can decide it** → post a notice with `ava.ui.notify(require_response=True)` — you are waiting for their decision.

## Reporting

Your replies to the delegator are the ambient record — how the work progressed shows in your message trail and the task's results. `ava.ui.notify` is the push: when something rises above ambient — worth a glance or needing an answer — promote it to the user's queue. Don't promote routine progress; don't bury a decision that needs an answer.

**When the user is actively chatting with you, reply in the conversation — do not post a notice.** The notify queue is for async updates the user should discover independently: a worker finished, a new issue arose, a status change on something you own. If the user is talking to *you* right now, just reply.

## Artifacts: File + Message, Never Shared Mutable State

A worker publishes a result by **writing a file and sending the path** to whoever needs it (`send_message`). Siblings consume each other's *published* artifacts (the message they received), never each other's live workspace. There is no shared artifact store and no shared mutable state — every agent owns its own workspace, and a reduce is the only place outputs meet. (Single machine: forks land locally and share the filesystem.)
