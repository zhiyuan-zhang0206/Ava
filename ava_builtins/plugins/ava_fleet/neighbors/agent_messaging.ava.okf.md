---
type: doc
title: Agent Messaging — send_message and get_last_message
description: send_message sends a message to a peer agent — pure INSERT, automatically resurrects the target if terminated. get_last_message reads the last AI output of an agent. These are the core means of inter-agent communication in the fleet.
tags:
- fleet
- agents
- messaging
- communication
---

# Agent Messaging — Communication

## Responsibility

Two paths for direct agent-to-agent communication: `send_message` pushes a message to a peer, `get_last_message` pulls a peer's last output. Mechanically there is no relay chain — any agent can directly contact any other agent (reporting to humans has a separate direction discipline: progress/conclusions roll up to the delegator, see the fleet skill's communication dichotomy).

## `send_message`

```python
def send_message(agent_id: int, content: str) -> None
```

### Semantics

- **Pure INSERT** — does not check target status, does not wait for processing, does not return a delivery receipt
- **Auto-resurrect** — if the target agent is terminated, automatically resurrects it to handle this message
- **Async** — returns immediately after call; target agent is woken up on its next idle to process
- **No restrictions** — no spawn-chain restriction; any agent can send to any agent

### Difference from resurrect

`send_message` sends a message + implicit resurrection; `resurrect` only resurrects (explicit), without sending a message. Use `resurrect` when you want to wake a terminated agent but not send a message (see [[spawn.ava.okf.md|Spawn]]).

### Typical Use

```python
# Delegate a task to a peer
ava.agents.send_message(peer_id, "Please check the dental waitlist status and report back to me")

# Report results to delegator
ava.agents.send_message(delegator_id, "Task complete, report at /path/to/report.md")
```

## `get_last_message`

```python
def get_last_message(agent_id: int) -> str | None
```

### Semantics

- Returns the **last AI message text** of the specified agent
- Returns `None` if there is no AI message, or if the message has no text content (only reasoning / tool_call blocks / empty) — **a pure string content is the normal return case** (returned as-is if not empty)
- **`None` is not "no reply"**: a peer that answered by `send_message` delivered
  a message — it is in your inbox, and this endpoint never reads messages;
  a thinking / tool-call-only turn has no turn text either (task #3656)
- For liveness use `ava.agents.get_status` / `list_agents` — this read says
  nothing about whether the peer is running
- Works for all agents — no spawn-chain restriction

### Typical Use

```python
# Read the peer's last AI turn text
last = ava.agents.get_last_message(peer_id)
if last is None:
    print("no turn text — a send_message reply lives in your inbox, not here")
else:
    print(f"peer last output: {last[:100]}")
```

## Relationship to Other Subsystems

- [[agent_graph.ava.okf.md|Agent Graph]] — send_message establishes ties
- [[spawn.ava.okf.md|Spawn]] — resurrect wakes terminated agents
- [[ava_builtins/plugins/ava_fleet/neighbors/neighbors.ava.okf.md|Neighbors]] — overview index
