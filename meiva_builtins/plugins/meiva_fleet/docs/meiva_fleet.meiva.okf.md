---
type: doc
title: meiva_fleet — Fleet Plugin Overview
description: "meiva_fleet is the entire surface of an agent facing human supervisors: self-presentation, notification system, task registry, inter-agent operations, and agent lifecycle management. Disabling this plugin puts the agent into fully autonomous mode."
tags:
- fleet
- plugin
- overview
---

# meiva_fleet — Fleet Plugin Overview

## What it is

`meiva_fleet` is the entire surface of an agent facing human supervisors. It registers all capabilities for agents to present themselves in the fleet monitoring view, as well as all primitives for inter-agent coordination and reporting to humans. Disabling this plugin puts agents into autonomous mode — presenting nothing to humans.

## Overall Architecture

```
┌─────────────────────────────────────────────────┐
│  meiva_fleet plugin                               │
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

- **Self** only injects `set_label` (declared as an `SdkMember` in `plugin.py`) — other members of core meiva.self are not affected by this plugin; **Notify**'s `ui.notify` family is entirely declared by `plugin.py`
- **Tasks** is implemented by `task_registry.py`, with update validation and database-write helpers in `_task_update.py`; the public registry remains the sub-module registered by the plugin
- **Neighbors** and **Spawn** are `meiva.agents` SDK core; the fleet plugin uses system prompts to let agents understand their usage conventions — the plugin itself does not register these methods (the only exception: the fleet-only `label=` parameter of `spawn` is added by `_spawn_with_label` monkeypatch)
- **Task Maintenance** is a gateway-side daemon registered by this plugin into the ops service roster (overdue task reminders + escalation) — declared via `services()` in `plugins/meiva_fleet/services.py` with `ServiceSpec`, discovered by `services/supervision/roster/plugin_services.py:plugin_services()` based on plugin code "existence" and folded into single-source `build_services()`. Note it is a machine-level daemon, **its life/death is not determined by agent-side plugin enable/disable** (that is the agent surface aspect); the host-owned gate is `FleetConfig.task_maintenance_enabled` in the plugin config image. See [[task_maintenance.meiva.okf.md|Task Maintenance]] for details
- **Skills**: the plugin also carries two skills (converge syncs to `~/.meiva/skills/meiva_fleet/`) — `meiva_fleet` (fleet collaboration outline, default injected into system prompt allowlist) + `reduce-context-switch-for-human` (interrupt discipline, added in #620). See [[meiva_builtins/plugins/meiva_fleet/docs/skills/skills.meiva.okf.md|Fleet Skills]] for details

## Two-layer Relationship

1. **Agent ↔ User**: via Self (activity trail, role labels) and Notify (notification queue) — agents present themselves and request decisions. Communication is **bifurcated**: authorization/decisions go directly to the user from any depth; progress/conclusions roll up along the delegation tree to the delegator for aggregation before entering the user queue
2. **Agent ↔ Agent**: via Neighbors (discovery, messages) and Spawn (creation, termination) — agents coordinate division of labor, no fixed hierarchy

## Dependencies

- All sub-concepts share the same DB (`agent_notices`, `agent_tasks` tables, etc.)
- Notify depends on Self's `agent_id` as the notice owner
- Tasks notifies relevant agents on owner change with a system note (`send_system_note`, NoteTag `task`)
- Agents spawned by Spawn establish a tie with the spawner via Neighbors

## Configuration

- Enable/disable uses per-machine `~/.meiva/plugins_config.json` (`base/packages/plugins/enable_config.py:set_local_enabled` is the only writer), CLI `meiva plugins enable/disable <name>` — no `MEIVA_FLEET_ENABLED` or similar env switch
- After disabling: `meiva.ui` falls back to pure page mode (no notifications), `meiva.self`'s log/label methods disappear, `meiva.tasks` namespace disappears, `spawn(label=…)` raises TypeError

## Workflow boundary

The runtime face also contributes `fleet_compact_reminder` through
`compact_reminder_sections`. On the soft compact reminder it asks agents
already coordinating work to preserve accepted assignments, reporting
commitments, blockers and artifact locations, and reconcile changed task
status. It does not require a new task or a notification just because a
context window is ending. Core owns the basic `to_user` maintenance reminder.

Fleet exposes optional collaboration and tracking capabilities. `meiva-workflow`
owns selection of direct execution, delegation, and orchestration. Enabling Fleet
or reading its skill does not require spawning peers, creating registry tasks,
setting a role label, or building a management tree. Task lifecycle rules apply
when an agent chooses task tracking or accepts an existing task. Delegated result
routing applies to an accepted assignment, not merely a spawn ancestry edge.

## Communication ownership

`meiva_builtins/plugins/meiva_fleet/agent_runtime.py:fleet_self_section` keeps the
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
for agents without Fleet. `meiva-being-a-long-running-agent` supplies waiting,
monitoring, recovery, and persistence procedures.

## Configuration ownership

`default_config.py` is the sole schema for the five Fleet policy fields: the host gate `task_maintenance_enabled`, cluster-pinned maintenance interval/backoff/escalation count, and cluster-pinned `reduce_context_switch`. Defaults and restart metadata remain unchanged; none accepts per-agent pins. The agent face reads its bound typed Fleet config; runners receive cluster policy via the bootstrap snapshot while host fields stay local. Gateway config images are cluster authority.

The pure config face declares the non-secret Core dependency `daemon.notice_ttl_limit_seconds`; escalation validates that declaration at its read boundary. DB/Redis are existing explicit resources, not flags. This migration does not retire other plugins' global binding or guarantee that every existing Core read has been converted.

Plugin writes reject invalid candidates and mixed-owner requests with `InvalidConfigOverlay` (panel 400). Only image CAS mismatches raise `PluginConfigChangedError` (panel 409). Both reject host RPC writes. Declaration, persisted-image, default and IO failures propagate unchanged.

`meiva plugins update` reconciles the declared schema in memory against the stored image. Pure readers never create a home. Repository merges do not migrate a real home or deploy.
