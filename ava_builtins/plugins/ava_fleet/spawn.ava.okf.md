---
type: doc
title: Spawn — Agent Lifecycle Management
description: "All operations of the agent lifecycle in ava.agents: spawn (create a new agent), fork (create by copying conversation state), terminate (terminate), restart (restart process), resurrect (resurrect a terminated agent), presets (named configuration templates)."
tags:
- fleet
- lifecycle
- spawn
- presets
---

# Spawn — Agent Lifecycle Management

## Responsibility

Agent creation, copying, termination, restart, resurrection — dynamic member management for the fleet. spawn/fork/terminate/restart/resurrect are `ava.agents` **core** methods, the fleet plugin does not register them. However, **the `label=` parameter of `spawn` is fleet-only**: wrapped around core spawn by `ava_builtins/plugins/ava_fleet/plugin.py:_spawn_with_label` (:554-584) via monkeypatch (**not** `register_namespace_member`); after disabling fleet, `spawn(label=…)` raises `TypeError`, and `create_and_assign` also disappears.

## API

### `ava.agents.spawn(prompt=None, fork_from=None, machine=None, config_overlay=None, label=None, preset=None) -> int`

Creates a new agent, returns its agent_id.

| Parameter | Description |
|------|------|
| `prompt` | First message; omit = agent idle after creation |
| `fork_from` | Copy that agent's conversation state to the new agent |
| `machine` | Target machine; omit = current machine (`ava.self.SELF_MACHINE_NAME`) |
| `config_overlay` | Per-field config override, e.g. `{"llm_model": "..."}` |
| `label` | Initial role/name; omit = auto-named. **fleet-only**: provided by fleet's spawn wrap; unavailable when fleet is disabled |
| `preset` | Preset config template name; when given together with `config_overlay`, the latter wins field-by-field |

### `ava.agents.fork_from` (parameter of spawn, not a standalone function)

`fork_from` is a parameter of `spawn`, not a standalone function. Pass the id of an existing agent, and the new agent will copy its full conversation state — suitable for splitting work: after the parent agent accumulates context, fork a child agent to continue a specific subtask.

### `ava.agents.terminate(agent_id: int, *, message: str | None = None, force: bool = False) -> TerminateResult`

Terminates an agent.

- Default: agent exits after completing the current turn
- `message`: retain a final message for the next resurrection without another response
- `force=True`: immediately kill the process

### `ava.agents.restart(agent_id: int) -> RestartResult`

Restart agent process. Agent completes the current turn then restarts as a new process, keeping the same agent_id.

### `ava.agents.resurrect(agent_id: int, prompt: str) -> ResurrectResult`

Resurrect a terminated agent. The agent retains its previous conversation state and receives `prompt` to understand why it was woken.

- `prompt` is required — lets the agent know the context
- Returns `ALREADY_ALIVE` for an already alive agent
- Raises `AgentNotFound` for a nonexistent agent_id

### `ava.agents.presets` — Configuration Templates

```python
# List all presets
ava.agents.presets.list() -> list[Preset]

# Get by name
ava.agents.presets.get(name: str) -> Preset
```

```python
class Preset:
    id: int
    name: str
    label: str
    description: str | None
    config: dict[str, object]   # config overrides
    created_at: datetime
    updated_at: datetime
```

Presets are used to reuse common configurations — for example, a "gmail-agent" preset pre-configures gmail skill, a specific model, and machine. `spawn(preset="gmail-agent")` creates an email processing agent.

## Lifecycle States

```
IDLING (unclaimed) ──claim──→ RUNNING ──→ IDLING
                │              │           │
                │              │  restart  │
                │              ↓           │
                │         RESTARTING ──→ IDLING (unclaimed)
                │
                ↓ (fails)
           TERMINATED ←── terminate (from any state)
                │
                └── resurrect ──→ IDLING (unclaimed; retains conversation state)
```

## Typical Usage Patterns

### Parallel Decomposition

```python
# Break down a large task into independent subtasks, spawn a worker for each
worker_ids = []
for task in sub_tasks:
    wid = ava.agents.spawn(
        prompt=task,
        config_overlay={"llm_model": "claude-sonnet-4-5"},
        label=f"worker-{task.topic}",
    )
    worker_ids.append(wid)

# Then idle, waiting for each worker to report results via send_message
```

### Fork Pattern

```python
# Current agent has accumulated context, fork a child agent to handle a specific direction
child_id = ava.agents.spawn(
    prompt="Continue analyzing this lead...",
    fork_from=ava.self.AGENT_ID,
    label="Lead Analyst",
)
```

## Relationship with Other Subsystems

- [[neighbors.ava.okf.md]] — spawn/fork/resurrect establish ties, terminate does not affect existing ties
- [[self.ava.okf.md]] — label can be specified during spawn
- [[tasks.ava.okf.md]] — task owner is an agent_id, agent must exist when changing
- [[ava_builtins/plugins/ava_fleet/ava_fleet.ava.okf.md]] — Overview
