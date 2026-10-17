---
type: doc
title: Fleet Skills — plugin-carried skills
description: "Two skills carried by the meiva_fleet plugin: meiva_fleet (fleet collaboration master outline — one-shot orchestrator pattern + task↔fleet interaction rules) and reduce-context-switch-for-human (interrupt discipline — default queuing, batch reporting, autonomous-push posture). Converge syncs into the load directory with the plugin name as the top-level namespace."
tags:
- fleet
- agent-instruction
- extensions
---

# Fleet Skills — plugin-carried skills

## What It Is

The `meiva_fleet` plugin, besides registering SDK surfaces (notify / tasks / self / spawn-label), **carries skills**: `plugins/meiva_fleet/skills/` is synced by converge to `~/.meiva/skills/meiva_fleet/` (plugin name = top-level namespace). Agents load them via `meiva.help(meiva.skills.meiva_fleet.<name>)`.

- [[meiva_builtins/plugins/meiva_fleet/skills/meiva-fleet/docs/meiva-fleet.meiva.okf.md|meiva_fleet]] — fleet collaboration master outline: orchestrator decomposition / fork worker / aggregation judgment + task↔fleet interaction rules + communication dichotomy
- [[meiva_builtins/plugins/meiva_fleet/skills/reduce-context-switch-for-human/docs/reduce-context-switch-for-human.meiva.okf.md|reduce-context-switch-for-human]] — interrupt discipline (added in #620): default queuing via `meiva.ui.notify`, batch by manager subtree, progress rolls up, push only for truly urgent matters

## Notes

- `meiva-fleet` is indexed in every agent's prompt, like every other loaded skill (`skills_to_inject_into_system_prompt` defaults to `*`) — fleet collaboration conventions are one `meiva.help` away from any agent; bodies are always pulled on demand.
- For the skill mechanism and origin axis (origin=plugin), see [[meiva/skills/docs/skills.meiva.okf.md|Skill System]]; for the plugin overview, see [[meiva_builtins/plugins/meiva_fleet/docs/meiva_fleet.meiva.okf.md|Meiva Fleet]].
