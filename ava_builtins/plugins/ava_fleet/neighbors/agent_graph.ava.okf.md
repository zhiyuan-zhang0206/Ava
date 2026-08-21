---
type: doc
title: Agent Graph — get_neighbors Relationship Graph and get_ancestors Spawn Chain
description: get_neighbors returns a list of associated agents sorted by tie strength (ties on spawn/fork/resurrect/send_message, decaying over time; depth controls hop count). get_ancestors returns the spawn/fork chain above an agent, nearest first. Terminated agents are included in both.
tags:
- fleet
- agents
- graph
- neighbors
---

# Agent Graph — `get_neighbors` and `get_ancestors`

## Responsibility

`ava.agents.get_neighbors(agent_id, depth=1, limit=20)` returns a list of peers with the strongest relationships to the target agent, sorted by tie strength in descending order. This is the core mechanism for agents in the fleet to discover each other — any agent can query the neighbor graph of any agent.

`ava.agents.get_ancestors(agent_id)` returns the spawn chain ABOVE an agent — the agents that spawned it, walking spawn/fork lineage upward to the top of the chain, nearest ancestor first. This is the responsibility-attribution read: when delegation looks misrouted, walk the ancestors of the agent in question to see whose chain it belongs to.

## API

```python
def get_neighbors(agent_id: int, depth: int = 1, limit: int = 20) -> list[Neighbor]
def get_ancestors(agent_id: int) -> list[Neighbor]
```

### Parameters

| Parameter | Default | Description |
|------|------|------|
| `agent_id` | required | ID of the query center agent |
| `depth` | `1` | Hops: 1 = direct ties, 2 = indirect ties (friends of friends) |
| `limit` | `20` | Maximum number of results returned |

`get_ancestors` takes no `depth`/`limit` — the chain walk always goes to the top (a spawn chain is a simple upward path).

### Returns: `class Neighbor`

```python
class Neighbor:
    agent_id: int        # agent ID (field name is agent_id, not id)
    label: str | None    # role label
    status: AgentStatus  # current status
    depth: int           # hops from the queried agent: out along ties for
    #                       neighbors, up the spawn chain for ancestors (1 = direct)
    score: float         # tie strength (higher = closer / more frequent);
    #                       ancestors: lineage edge weight (spawn/fork counts)
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

## Ancestors

The ancestor chain is built from **directed** spawn/fork events only: the
event stream writes them as `agent_id` = the new agent, `target_agent_id` =
its spawner, so the walk follows `agent_id → target_agent_id` upward.
Message ties never form ancestors, and `resurrect` wakes an existing agent
rather than creating one, so it is excluded too. An agent spawned by the
user (spawner not an agent) has no ancestor edge — `get_ancestors` returns
`[]`.

```python
# See who spawned me and who spawned them
chain = ava.agents.get_ancestors(ava.self.AGENT_ID)
for a in chain:
    print(a.agent_id, a.label, a.depth)
```

## Relationship with Other Subsystems

- [[agent_messaging.ava.okf.md|Agent Messaging]] — send_message establishes ties
- [[spawn.ava.okf.md|Spawn]] — spawn/fork/resurrect establish ties
- [[ava_builtins/plugins/ava_fleet/neighbors/neighbors.ava.okf.md|Neighbors]] — overview index
