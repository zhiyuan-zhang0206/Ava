---
type: doc
title: Agent Directory — list_agents / list_machines / commands
description: List all agents and machines in the fleet. list_agents filters by status, list_machines queries machine liveness, commands lists slash commands accepted by peers. Includes AgentRow, AgentStatus, Machine data class definitions.
tags:
- fleet
- agents
- directory
- discovery
---

# Agent Directory — Directory Queries

## Responsibility

List agents and machines in the fleet — not based on the relationship graph (that's the [[agent_graph.ava.okf.md|Agent Graph]]'s job), but a global directory. Used to discover available peer agents and machine resources.

## `list_agents`

```python
def list_agents(
    filter_by_status: tuple[AgentStatus, ...] | None = (RUNNING, IDLING)
) -> list[AgentRow]
```

Filter the agent list by status. Default returns only `RUNNING` and `IDLING` agents; pass `None` to list all (including `TERMINATED`).

### `class AgentRow`

```python
class AgentRow:
    agent_id: int
    label: str | None              # role label
    status: AgentStatus            # current status
    spawner: str                   # creator (agent ID or "user")
    machine: str                   # machine name where it lives
    spawned_at: datetime           # creation time
    started_at: datetime | None    # process start time
    last_active_at: datetime       # last activity time
    pid: int | None                # OS process ID
    heartbeat_paused_until: datetime | None
```

### `AgentStatus` Enum

| Value | Meaning |
|----|------|
| `ALLOCATED` | Allocated, not yet started |
| `STARTING` | Starting up |
| `RUNNING` | Executing |
| `IDLING` | Idle, waiting for wake-up |
| `RESTARTING` | Restarting |
| `TERMINATED` | Terminated |

## `list_machines`

```python
def list_machines() -> list[Machine]
```

List all machines in the cluster.

### `class Machine`

```python
class Machine:
    name: str
    description: str | None
    live: bool   # probed in real time at call time, not cached
```

## `commands`

```python
def commands() -> list[CommandInfo]
```

List slash commands accepted by peer agents. Activated by sending a `/name <instruction>` format message.

### `class CommandInfo`

```python
class CommandInfo:
    name: str             # command name (without /)
    description: str      # function description
    instruction_hint: str # parameter hint
```

## Typical Use

```python
# Find available idle agents
agents = ava.agents.list_agents()  # default RUNNING + IDLING
for a in agents:
    print(f"#{a.agent_id} {a.label or '(no label)'} on {a.machine}")

# Check machine liveness
for m in ava.agents.list_machines():
    if not m.live:
        print(f"⚠️ {m.name} offline")

# Query commands accepted by peers
for cmd in ava.agents.commands():
    print(f"/{cmd.name} — {cmd.description}")
```

## Relationship to Other Subsystems

- [[agent_graph.ava.okf.md|Agent Graph]] — based on relationship strength, not global directory
- [[agent_messaging.ava.okf.md|Agent Messaging]] — communicate with discovered peers
- [[ava_builtins/plugins/ava_fleet/neighbors/neighbors.ava.okf.md|Neighbors]] — overview index
