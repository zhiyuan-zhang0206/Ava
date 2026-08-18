# Fleet

Multi-agent is native to Ava, not bolted on. Any agent can `spawn` a peer,
`fork` another agent's explored context, and `send_message` it directly —
all peers, no central scheduler.

## Why it matters

- **Decentralized** — no single node owns the graph, so no single failure
  strands it. A goal-watcher wakes a supervisor the moment a worker stalls; a
  terminated agent auto-resurrects to handle an incoming message.
- **Findable** — ties form on spawn / fork / message and fade with time;
  `get_neighbors` ranks who to talk to by tie strength, not by rank.

## Human interface — the notification queue

Every agent's notifications — progress, FYIs, decisions that need the user's
call — converge into **one aggregated asynchronous queue** (`ava.ui.notify`,
`edit_notice`, `dismiss_notice`). One open notice per agent, graded
FYI-vs-needs-response, optionally blocking, editable and dismissible. A human
supervises the whole fleet asynchronously, from one queue, without being in a
live conversation with any agent.

## Organization — the task graph

Agents organize across conversations through the **shared persistent task
graph** (`ava.tasks`): parent/child task trees, owner assignment, progress
reminders that cannot be disabled, and an explicit status flow. The task
registry is registered by the fleet plugin (`ava.register_namespace("tasks",
task_registry)` in `ava_builtins/plugins/ava_fleet/plugin.py`) — the fleet's
organizational surface rides on the plugin layer, alongside the notification
queue and agent labels.

## How it works

<!-- TODO(image): fleet graph — spawn/fork/message edges + notification queue + task tree -->

```python
worker = ava.agents.spawn(
    prompt="Refactor the auth module; tests must stay green.",
    fork_from=ava.self.AGENT_ID,   # inherit context — the edge is the dependency graph
    label="auth-refactor",
)
ava.agents.send_message(worker, "Skip the legacy SAML path — out of scope.")
for n in ava.agents.get_neighbors(ava.self.AGENT_ID, depth=1):
    print(n)   # #id <label> <status> — strongest ties first
```

## Design decisions

- [Agents become detached native processes](../../decisions/2026-07-19-agents-become-detached-native-processes.md)
- [Crash auto-resurrect](../../decisions/2026-07-21-crash-auto-resurrect.md)
- [One send is one inbound](../../decisions/2026-08-01-one-send-is-one-inbound.md)
