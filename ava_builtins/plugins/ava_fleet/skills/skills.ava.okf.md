---
type: doc
title: Fleet Skills — plugin-carried skills
description: "Two skills carried by the ava_fleet plugin: ava_fleet (fleet collaboration master outline — one-shot orchestrator pattern + task↔fleet interaction rules) and reduce-context-switch-for-human (interrupt discipline — default queuing, batch reporting, autonomous-push posture). Converge syncs into the load directory with the plugin name as the top-level namespace."
tags:
- fleet
- agent-instruction
- extensions
---

# Fleet Skills — plugin-carried skills

## What It Is

The `ava_fleet` plugin, besides registering SDK surfaces (notify / tasks / self / spawn-label), **carries skills**: `plugins/ava_fleet/skills/` is synced by converge to `~/.ava/skills/ava_fleet/` (plugin name = top-level namespace). Agents load them via `ava.help(ava.skills.ava_fleet.<name>)`.

- [[ava_builtins/plugins/ava_fleet/skills/ava-fleet/ava-fleet.ava.okf.md|ava_fleet]] — fleet collaboration master outline: orchestrator decomposition / fork worker / aggregation judgment + task↔fleet interaction rules + communication dichotomy
- [[ava_builtins/plugins/ava_fleet/skills/reduce-context-switch-for-human/reduce-context-switch-for-human.ava.okf.md|reduce-context-switch-for-human]] — interrupt discipline (added in #620): default queuing via `ava.ui.notify`, batch by manager subtree, progress rolls up, push only for truly urgent matters

## Notes

- `ava-fleet` is indexed in every agent's prompt, like every other loaded skill (`skills_to_inject_into_system_prompt` defaults to `*`) — fleet collaboration conventions are one `ava.help` away from any agent; bodies are always pulled on demand.
- For the skill mechanism and origin axis (origin=plugin), see [[ava/skills.ava.okf.md|Skill System]]; for the plugin overview, see [[ava_builtins/plugins/ava_fleet/ava_fleet.ava.okf.md|Ava Fleet]].
