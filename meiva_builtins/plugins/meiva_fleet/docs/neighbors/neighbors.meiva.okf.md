---
type: doc
title: Neighbors — Inter-Agent Communication and Discovery
description: "Methods in meiva.agents for inter-agent communication and discovery: get_neighbors discovers peers by relationship strength, get_ancestors walks the spawn chain above an agent, send_message direct messaging, get_last_message reads output, list_agents/list_machines lists agents and machines."
tags:
- fleet
- agents
- coordination
- messaging
---

# Neighbors — Inter-Agent Communication and Discovery

## Responsibility

Inter-agent interaction is the communication layer of the fleet — how agents discover each other, send messages, and check status. Mechanically there is no relay chain: any agent can directly contact any other agent. But reporting to humans has direction (#620 dichotomy): authorization/decisions go directly to the user; progress/conclusions roll up to the delegator along the delegation tree (see [[meiva_builtins/plugins/meiva_fleet/skills/reduce-context-switch-for-human/docs/reduce-context-switch-for-human.meiva.okf.md|the interrupt discipline skill]]).

## Relationship to Other Subsystems

- [[spawn.meiva.okf.md|Spawn]] — spawn/fork/resurrect establish ties
- [[meiva_builtins/plugins/meiva_fleet/docs/tasks/tasks.meiva.okf.md|Tasks]] — system-note notification (NoteTag `task`) on task owner change
- [[meiva_builtins/plugins/meiva_fleet/docs/self.meiva.okf.md|Self]] — discover peers via label
- [[meiva_builtins/plugins/meiva_fleet/docs/meiva_fleet.meiva.okf.md|Meiva Fleet]] — overview
