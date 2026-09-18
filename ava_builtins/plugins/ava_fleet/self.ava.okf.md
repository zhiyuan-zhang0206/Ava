---
type: doc
title: Self — Agent Self-Presentation
description: "ava_fleet injects fleet members into ava.self: role label (set_label). Heartbeat pause/memory compaction/lifecycle (pause_heartbeat/compact/restart/terminate/update) are core ava.self, not provided by this plugin."
tags:
- fleet
- self
- agent-identity
---

# Self — Agent Self-Presentation

## Responsibility

ava_fleet injects the `set_label` fleet member into `ava.self` (`plugins/ava_fleet/plugin.py` `register_namespace_member`) — letting agents report their role to the fleet monitoring view and to their own agent-ID context note. The label that humans see for each agent when scanning the fleet view is provided by this member.

> **Boundary**: The **core** members of `ava.self` (AGENT_ID / MACHINE_SPEC / SELF_MACHINE_NAME / pause_heartbeat / compact / restart / terminate / update) are provided by `ava/self.py` and remain even if fleet is disabled — see documentation at [[ava/self.ava.okf.md|ava.self]]. Disabling fleet only removes the members below.

## API

### `ava.self.set_label(text: str) -> None`

Sets the agent's role label — the name displayed in the fleet graph.

- Label should be stable (role name, e.g., "health steward", "Memory pool maintenance"), not a task summary (changing every few minutes) and not a status line (current status goes in replies / task results)
- Task names are legitimate labels for ephemeral workers — a worker's "role" is its task
- Other agents discover and locate peers by label through `get_neighbors`, and read the responsibility chain above you through `get_ancestors`
- Once set, it is not automatically replaced; explicit call needed to change
- The label is stated in the agent's own agent-ID context note at every window
  establishment — agents read their current label there, and it is how a peer
  renders your role without a getter call

## Data Model

```
agents (table)
├── label         — role label (mutable by the agent itself)
└── label_user_set — whether explicitly set by agent (when TRUE, won't be auto-replaced)
```

(`agent_activity` remains for historical activity display but has no SDK writer since `ava.self.log` was removed 2026-08-02.)

## Dependencies

- DB table `agents`
- `publish_agent_updated_sync()` — pushes to fleet monitoring view after set_label

## Relationship to Other Subsystems

- [[notify.ava.okf.md]] — notify's notices have agent_id as owner
- [[neighbors.ava.okf.md]] — other agents discover this agent by label
- [[ava_builtins/plugins/ava_fleet/ava_fleet.ava.okf.md]] — overview
