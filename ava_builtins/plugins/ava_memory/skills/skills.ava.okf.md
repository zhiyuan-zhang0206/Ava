---
type: doc
title: Ava Memory Skills — skills carried by the plugin
description: "ava_memory plugin is the skill source for memory capability: the SKILL.md at the root of skills is the Memory Steward maintenance manual (loaded flat as ava.skills.ava_memory — daily merge, health check, note consolidation, query service, user-dimension maintenance), with a sub-skill consolidation (`ava memory` CLI daily consolidation workflow)."
tags:
- extensions
- agent-instruction
---

# Ava Memory Skills — skills carried by the plugin

## What is it

`ava_memory` plugin is the skill **source** for memory capability (plugin as source — the previously identically named `skills/ava_memory/` in the repo core has been deleted, resolving the naming collision of the mount name `ava_memory`). The **root of `plugins/ava_memory/skills/`** directly contains `SKILL.md`: the Memory Steward maintenance manual, which converge loads flat as a root skill `ava.skills.ava_memory` — daily merge, health check, agent note consolidation, and query services for other agents.

It has one sub-skill:

- [[ava_builtins/plugins/ava_memory/skills/consolidation/consolidation.ava.okf.md|consolidation]] —
  `ava.skills.ava_memory.consolidation`: the daily consolidation workflow of the shared memory pool (`ava memory` CLI one-step commit / push / refresh index; single-box self-managed, multi-host uses arbiter + per-machine steward)

Beyond the agent dimension, the root skill also maintains the **user
dimension** — repeatedly expressed preferences, recurring habits, corrected
behaviors, what the user values — as standing, continuously-maintained notes
(`user-profile.md`, `user-preference-rules-v2.md`,
`collaboration-preferences.md`, `user-core-principle.md`) rather than
one-off records; the Arbiter owns their coherence during consolidation.

## Notes

- Unlike the two-level pattern of ava_code / ava_fleet where "plugin name = top-level directory, skills hang under it", the skills root of this plugin **is itself a skill** (with SKILL.md at root): `ava_memory` is directly the root skill name, with sub-skills hanging under it.
- For the skill mechanism and origin axis (origin=plugin), see [[ava/skills.ava.okf.md|Skill System]]; for a line in the ops skill group overview, see [[ava_builtins/skills/ops_lifecycle/ops_lifecycle.ava.okf.md|ops skill group]]; for the hook surface of the plugin, see [[ava_builtins/plugins/ava_memory/ava_memory.ava.okf.md|ava_memory plugin]].
