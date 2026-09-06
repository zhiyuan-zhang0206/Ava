---
type: doc
title: ava.agents — Agent Interop
description: '`ava.agents` provides inter-agent communication capabilities — spawn new agents, send messages, query status, manage lifecycle. This is the infrastructure for fleet mode.'
tags:
- agent-view
- sdk
- agent-lifecycle
---

# ava.agents — Agent Interop

## What it is

`ava.agents` provides inter-agent communication capabilities — spawn new agents, send messages, query status, manage lifecycle. This is the infrastructure for fleet mode.

## Core API

### Lifecycle
- `spawn(prompt=None, fork_from=None, machine=None, config_overlay=None, preset=None) → int` — start a new agent, returns agent ID, non-blocking. `prompt` is the first message (should be self-contained); `fork_from` copies the parent agent's conversation state; `machine` defaults to self; `preset` loads config from a preset template, when passed with `config_overlay` the latter overrides field-by-field. `config_overlay={"eval_isolation": true}` starts an eval-isolated agent; `eval_network_allowlist` can explicitly retain `web` or `understand`, while `mcps` and `ui` have no allowlist. Identity-class config you do NOT name (model, reasoning effort, skill set, prompt shaping) is resolved from the cluster default at spawn and frozen onto the new agent for its life — a later default change never re-brains it. (The `ava_fleet` plugin appends `label=` parameter to `spawn` to set initial role label.)
- `terminate(agent_id, *, message=None, force=False) → TerminateResult` — agent exits after completing the current turn; `message` is retained without another response and is visible after resurrection; `force=True` kills the process immediately.
- `restart(agent_id) → RestartResult` — agent restarts as a fresh process with the same ID after completing the current turn.
- `resurrect(agent_id, prompt) → ResurrectResult` — wake up a terminated agent, preserving its conversation state; `prompt` required.

### Communication
- `send_message(agent_id, content)` — send a message to a peer agent. If target is terminated, automatically resurrect. Pure INSERT — no state check, no waiting, no delivery receipt.
- `commands() → list[CommandInfo]` — list slash commands accepted by the peer agent; send `/name <command>` to invoke. One message may invoke several (`/a … /b …`) — the receiver expands the whole chain in order inside that single message. Every command expands the same way; no combination is special-cased or refused.
- `get_last_message(agent_id) → str | None` — read the last AI message text from the agent (None if none).

### Discovery
- `list_agents(filter_by_status=(RUNNING, IDLING)) → list[AgentRow]` — list agents filtered by status through the summary roster projection; fetch one agent when full lifecycle details are needed.
- `get_status(agent_id) → AgentStatus` — current status of a single agent (alternative to list_agents then manual filter).
- `list_machines() → list[Machine]` — list all machines and their alive status.
- `get_neighbors(agent_id, depth=1, limit=20) → list[Neighbor]` — related agents ranked by connection strength. Connections are established on spawn/fork/resurrect/send_message and decay over time.
- `get_ancestors(agent_id) → list[Neighbor]` — the spawn/fork chain ABOVE an agent (who spawned whom), nearest ancestor first; `depth` = hops up. Message ties never form ancestors; a user-spawned agent returns [].

### Data Types
- `AgentRow`: agent_id, label, status, spawner, fork_source_agent_id, machine, spawned_at, started_at, last_active_at, last_inbound_at, pid, supports_vision, liveness_state, notices_awaiting_response, unread_notice_count, heartbeat_paused_until
- `AgentStatus`: RUNNING / IDLING / RESTARTING / TERMINATED — four states, no ops-only states to project away.
- `Neighbor`: agent_id, label, status, depth (hops from the queried agent — out for neighbors, up for ancestors), score (connection strength)
- `Machine`: name, description, live (detected at call time, not cached)

## Key Dependencies
- [[state.ava.okf.md]] — agent state storage
- [[lifecycle.ava.okf.md]] — process lifecycle
- [[gateway-cli.ava.okf.md]] — gateway is the actual entry point for agent spawn

## Notes
`send_message` is asynchronous insert — returns immediately, does not wait for target agent to receive or process. Target agent is woken if idle.
