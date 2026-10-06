---
type: doc
title: ava_fleet — Fleet Plugin Overview
description: "ava_fleet is the entire surface of an agent facing human supervisors: self-presentation, notification system, task registry, inter-agent operations, and agent lifecycle management. Disabling this plugin puts the agent into fully autonomous mode."
tags:
- fleet
- plugin
- overview
---

# ava_fleet — Fleet Plugin Overview

## What it is

`ava_fleet` is the entire surface of an agent facing human supervisors. It registers all capabilities for agents to present themselves in the fleet monitoring view, as well as all primitives for inter-agent coordination and reporting to humans. Disabling this plugin means the agent enters fully autonomous mode — no information is presented to humans.

## Overall Architecture

```
┌─────────────────────────────────────────────────┐
│  ava_fleet plugin                               │
│                                                 │
│  ┌──────────┐  ┌──────────┐  ┌───────────────┐ │
│  │ Self     │  │ Notify   │  │ Tasks         │ │
│  │ (Self-presentation)│  │(Notification System)│  │ (Task Registry) │
│  └──────────┘  └──────────┘  └───────────────┘ │
│                                                 │
│  ┌──────────────┐  ┌─────────────────────────┐  │
│  │ Neighbors    │  │ Spawn                   │  │
│  │ (Agent Interop)│  │ (Agent Lifecycle)       │  │
│  └──────────────┘  └─────────────────────────┘  │
└─────────────────────────────────────────────────┘
```

- **Self** only injects `set_label` (declared as an `SdkMember` in `plugin.py`) — other members of core ava.self are not affected by this plugin; **Notify**'s `ui.notify` family is entirely declared by `plugin.py`
- **Tasks** is implemented by `task_registry.py`, with update validation and database-write helpers in `_task_update.py`; the public registry remains the sub-module registered by the plugin
- **Neighbors** and **Spawn** are `ava.agents` SDK core; the fleet plugin uses system prompts to let agents understand their usage conventions — the plugin itself does not register these methods (the only exception: the fleet-only `label=` parameter of `spawn` is added by `_spawn_with_label` monkeypatch)
- **Task Maintenance** is a gateway-side daemon registered by this plugin into the ops service roster (overdue task reminders + escalation) — declared via `services()` in `plugins/ava_fleet/services.py` with `ServiceSpec`, discovered by `ops/spec.py:plugin_services()` based on plugin code "existence" and folded into single-source `build_services()`. Note it is a machine-level daemon, **its life/death is not determined by agent-side plugin enable/disable** (that is the agent surface aspect); the cluster-level switch is the explicit settings field `AVA_TASK_MAINTENANCE_ENABLED`. See [[task_maintenance.ava.okf.md|Task Maintenance]] for details
- **Skills**: the plugin also carries two skills (converge syncs to `~/.ava/skills/ava_fleet/`) — `ava_fleet` (fleet collaboration outline, default injected into system prompt allowlist) + `reduce-context-switch-for-human` (interrupt discipline, added in #620). See [[ava_builtins/plugins/ava_fleet/docs/skills/skills.ava.okf.md|Fleet Skills]] for details

## Two-layer Relationship

1. **Agent ↔ User**: via Self (activity trail, role labels) and Notify (notification queue) — agents present themselves and request decisions. Communication is **bifurcated**: authorization/decisions go directly to the user from any depth; progress/conclusions roll up along the delegation tree to the delegator for aggregation before entering the user queue
2. **Agent ↔ Agent**: via Neighbors (discovery, messages) and Spawn (creation, termination) — agents coordinate division of labor, no fixed hierarchy

## Dependencies

- All sub-concepts share the same DB (`agent_notices`, `agent_tasks` tables, etc.)
- Notify depends on Self's `agent_id` as the notice owner
- Tasks notifies relevant agents on owner change with a system note (`send_system_note`, NoteTag `task`)
- Agents spawned by Spawn establish a tie with the spawner via Neighbors

## Configuration

- Enable/disable uses per-machine `~/.ava/plugins_config.json` (`base/packages/plugins/enable_config.py:set_local_enabled` is the only writer), CLI `ava plugins enable/disable <name>` — no `AVA_FLEET_ENABLED` or similar env switch
- After disabling: `ava.ui` falls back to pure page mode (no notifications), `ava.self`'s log/label methods disappear, `ava.tasks` namespace disappears, `spawn(label=…)` raises TypeError

## Workflow boundary

Fleet exposes optional collaboration and tracking capabilities. `ava-workflow`
owns selection of direct execution, delegation, and orchestration. Enabling Fleet
or reading its skill does not require spawning peers, creating registry tasks,
setting a role label, or building a management tree. Task lifecycle rules apply
when an agent chooses task tracking or accepts an existing task. Delegated result
routing applies to an accepted assignment, not merely a spawn ancestry edge.

## Communication ownership

`ava_builtins/plugins/ava_fleet/agent_runtime.py:_fleet_self_section` keeps the
optional capability entry point and essential delivery semantics resident:
conversation output is not peer delivery, useful messages go to action owners,
protocol receipts are distinct from completion, duplicate reporting is avoided,
and user decisions/results reach the appropriate live or asynchronous channel.

The Fleet skill's `reference/operating-contract.md` owns the detailed label,
notice, delegation, completion, and task lifecycle procedures. Load the skill and
applicable contract when choosing those capabilities; those procedures are not
repeated in every system prompt. `reduce-context-switch-for-human` owns reporting
cadence, aggregation, and interruption procedures; the resident toggle-gated
section preserves only the interruption boundary and notice replacement fact.
Turning that toggle off does not remove peer-delivery semantics.

The core prompt owns general lifecycle and operating cost discipline, including
for agents without Fleet. `ava-being-a-long-running-agent` supplies waiting,
monitoring, recovery, and persistence procedures.
