---
type: doc
title: Operations, Scheduling, and Lifecycle Skills
description: A set of skills covering operating and extending Ava, scheduling timed tasks, managing background watchers, behavioral discipline for long-running agents and ultra-speed workers, and maintaining the memory pool. Core built-in — ava_memory is carried by the ava_memory plugin (origin=plugin), others origin=repo.
tags:
- extensions
- agent-instruction
---

# Operations, Scheduling, and Lifecycle Skills

## What it is
A set of skills centered around **running, extending, and maintaining** Ava's own operations, as well as agent behavioral discipline: operating/extending itself via CLI, turning natural language into managed scheduled tasks, starting event/time-triggered background watchers, behavioral discipline as a long-running process/ultra-speed worker, and maintaining a long-term memory pool. Core built-in — ava_memory is carried by the ava_memory plugin (origin=plugin), others origin=repo.

| Skill | Purpose | Details |
|------|------|------|
| ava-guide | Operate / extend yourself via the `ava` CLI; root SKILL.md is an index, **seven** bare-name sub-skills bear the load: `ops` (start/stop/update cluster, cluster/unit/machine model, channel, release), `mcp` (install/manage MCP servers), `packages` (install/manage skills/plugins), `agents` (agent/command/preset/schedule concepts), `presets` (create/modify/manage agent config presets, d31660c8), `models` (model tier judgment + cost policy), `onboarding` (first use with a new user — preference interview, intent discovery, memory write-up) | [[ava_builtins/skills/ops_lifecycle/ava-guide.ava.okf.md]] |
| ava-schedule-writer | Natural language → gateway managed scheduled task (resumable script + `/api/schedules`) | [[ava_builtins/skills/ops_lifecycle/ava-schedule-writer.ava.okf.md]] |
| ava-watcher | Start a background watcher that wakes you on event/time triggers (stop in-turn polling) | [[ava_builtins/skills/ops_lifecycle/ava-watcher.ava.okf.md]] |
| ava-being-a-long-running-agent | Operating as a long-running process: manage lifecycle, wait for external events, persist before compaction | [[ava_builtins/skills/ops_lifecycle/ava-being-a-long-running-agent.ava.okf.md]] |
| ava-ultra-speed | Speed discipline for ultra-fast turnover workers: report as you go, never wait silently (#773, typical expand-at-start preload) | [[ava_builtins/skills/ops_lifecycle/ava-ultra-speed.ava.okf.md]] |
| ava_memory | Memory pool maintenance: daily merging, health checks, agent note consolidation, query service (carried by plugin; daily merge via CLI in `consolidation` sub-skill) | [[ava_builtins/plugins/ava_memory/ava_memory.ava.okf.md]] |

## Key Dependencies
- [[ava/skills.ava.okf.md|Skill System]] — skill mechanism and core-vs-instance origin axis
- [[ava/watcher.ava.okf.md|ava.watcher]] — ava-watcher / ava-schedule-writer rest on the `ava.watcher` SDK + gateway schedules
- [[ava_builtins/plugins/ava_memory/memory-api.ava.okf.md]] — ava_memory operates the `ava.memory` memory pool
