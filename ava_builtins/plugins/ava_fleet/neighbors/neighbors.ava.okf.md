---
type: doc
title: Neighbors — Inter-Agent Communication and Discovery
description: "Methods in ava.agents for inter-agent communication and discovery: get_neighbors discovers peers by relationship strength, get_ancestors walks the spawn chain above an agent, send_message direct messaging, get_last_message reads output, list_agents/list_machines lists agents and machines."
tags:
- fleet
- agents
- coordination
- messaging
---

# Neighbors — Inter-Agent Communication and Discovery

## Responsibility

Inter-agent interaction is the communication layer of the fleet — how agents discover each other, send messages, and check status. Mechanically there is no relay chain: any agent can directly contact any other agent. But reporting to humans has direction (#620 dichotomy): authorization/decisions go directly to the user; progress/conclusions roll up to the delegator along the delegation tree (see [[ava_builtins/plugins/ava_fleet/skills/reduce-context-switch-for-human/reduce-context-switch-for-human.ava.okf.md|the interrupt discipline skill]]).

## Relationship to Other Subsystems

- [[spawn.ava.okf.md|Spawn]] — spawn/fork/resurrect establish ties
- [[ava_builtins/plugins/ava_fleet/tasks/tasks.ava.okf.md|Tasks]] — send_message notification on task owner change
- [[ava_builtins/plugins/ava_fleet/self.ava.okf.md|Self]] — discover peers via label
- [[ava_builtins/plugins/ava_fleet/ava_fleet.ava.okf.md|Ava Fleet]] — overview
