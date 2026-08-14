---
type: doc
title: Agent Graph — get_neighbors Relationship Graph
description: get_neighbors returns a list of associated agents sorted by tie strength. Ties are established on spawn/fork/resurrect/send_message and decay over time. depth controls hop count; terminated agents are also included in results.
tags:
- fleet
- agents
- graph
- neighbors
---

# Agent Graph — `get_neighbors`

## Responsibility

`ava.agents.get_neighbors(agent_id, depth=1, limit=20)` returns a list of peers with the strongest relationships to the target agent, sorted by tie strength in descending order. This is the core mechanism for agents in the fleet to discover each other — any agent can query the neighbor graph of any agent.

## API

```python
def get_neighbors(agent_id: int, depth: int = 1, limit: int = 20) -> list[Neighbor]
```

### Parameters

| Parameter | Default | Description |
|------|------|------|
| `agent_id` | required | ID of the query center agent |
| `depth` | `1` | Hops: 1 = direct ties, 2 = indirect ties (friends of friends) |
| `limit` | `20` | Maximum number of results returned |

### Returns: `class Neighbor`

```python
class Neighbor:
    agent_id: int        # agent ID (field name is agent_id, not id)
    label: str | None    # role label
    status: AgentStatus  # current status
    depth: int           # hops (1 = direct)
    score: float         # tie strength (higher = closer / more frequent)
```

## Tie Mechanism

### Establishment

A tie is **established or strengthened** between two agents when any of the following interactions occur:

- `spawn` — parent agent spawns child agent
- `fork` — parent agent forks child agent
- `resurrect` — resurrect a terminated agent
- `send_message` — send a message to another agent

### Decay

Ties are not permanent — they decay over time. The more recent and frequent the interaction, the higher the `score`. Terminated agents remain in the graph; their ties continue to decay but are never removed.

### Multi-hop

With `depth=2`, second-degree neighbors (neighbors of neighbors) are returned. The greater the depth, the lower the `score` typically is, because indirect ties are weaker than direct ones.

## Typical Usage

```python
# Find an agent responsible for a domain
neighbors = ava.agents.get_neighbors(ava.self.AGENT_ID, depth=2)
for n in neighbors:
    if n.label == "Health Steward" and n.status in ("idling",):
        ava.agents.send_message(n.agent_id, "Please check the dental waitlist")
        break
```

## Relationship with Other Subsystems

- [[agent_messaging.ava.okf.md|Agent Messaging]] — send_message establishes ties
- [[spawn.ava.okf.md|Spawn]] — spawn/fork/resurrect establish ties
- [[ava_builtins/plugins/ava_fleet/neighbors/neighbors.ava.okf.md|Neighbors]] — overview index
