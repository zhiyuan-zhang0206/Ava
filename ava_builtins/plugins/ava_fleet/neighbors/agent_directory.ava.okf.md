---
type: doc
title: Agent Directory — list_agents / list_machines / commands
description: Search bounded pages of agents and list cluster machines. Directory scope separates live agents from history; cursors explicitly request more rows.
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

`list_agents` returns an `AgentDirectoryPage` with `agents` and `next_cursor`.
The default scope is `live`; use `terminated` for the archive or `all` for
cross-lifecycle discovery. `query` searches the directory on the server.
Rows are newest first. Pass the returned cursor as `before_id` to read the
next page, keeping the same scope and query; `next_cursor=None` ends the
result. `limit` defaults to 100 and cannot exceed 200.

A page is not the complete directory. Consumers that genuinely need every
matching agent explicitly iterate pages; finding a particular role should
start with a server-side search. Select an agent by ID to read its detail.

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
# Search one bounded page of live peers.
page = ava.agents.list_agents(scope="live", query="reviewer")
for agent in page.agents:
    print(f"#{agent.agent_id} {agent.label or '(no label)'} on {agent.machine}")
if page.next_cursor is not None:
    next_page = ava.agents.list_agents(
        scope="live", query="reviewer", before_id=page.next_cursor,
    )

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
