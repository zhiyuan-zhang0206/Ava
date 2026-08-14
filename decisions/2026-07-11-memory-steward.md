# Memory Steward — Role Initialization Record

## Decision
Create Memory Steward as a shared infrastructure PoC for Ava Corp, responsible for health maintenance of the `ava.memory` pool.

## Background
Previously, maintenance of the memory pool was intermittently performed by temporary worker agents (#778, #1137, #1398) acting as memory-arbiter.
After completing their tasks, these agents terminated, resulting in:
- Irregular merge rhythm (relying on manual triggering)
- No continuous health monitoring
- Other agents lacked a stable memory query entry point
- OKF formatting changes accumulated and were not committed

## Decision Details
1. Memory Steward as a long-running agent (non-terminating), label: `memory-steward`
2. Decoupled from Ava Corp's AI Infra PoC, as an independent Shared infra PoC
3. Responsibilities: daily merge, health check, agent note merge, query service
4. Automatically trigger daily tasks via cron watcher

## Implementation
- Skill: `skills/ava_memory/SKILL.md` (PR #375)
- Agent: #1609
- Watchers: 3 crons (3AM merge, 10AM health check, 12PM PR merge)

## Date
2026-07-11

> 2026-07-20: skill moved into plugin — now at `plugins/ava_memory/skills/SKILL.md` (original repo path
> `skills/ava_memory/` once shadowed the consolidation skill carried by the plugin during converge loading due to name collision; the latter is now
> `ava.skills.ava_memory.consolidation` sub-skill).
