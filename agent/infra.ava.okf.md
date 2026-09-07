---
type: doc
title: Infrastructure
description: Ava's infrastructure layer—provides underlying capabilities for agent runtime such as persistence, communication, observability, service lifecycle management. Covers database layer (Postgres), cache layer (Redis), MCP
tags: []
---

# Infrastructure

## What it is

Ava's infrastructure layer—provides underlying capabilities for agent runtime such as persistence, communication, observability, service lifecycle management. Covers database layer (Postgres), cache layer (Redis), shared MCP protocol service, LLM usage observation, and a set of backend service daemons.

## Core Responsibilities

- **Persistence**: LangGraph checkpoint of agent message history, inbound message queue, agent metadata
- **Checkpoint schema authority**: fresh install alone owns `PostgresSaver.setup()`; later upstream schema versions must be mirrored by paired Ava migrations, and every `ava start` capability verifies the complete applied version set read-only. Agent boot and checkpoint request/read paths perform no DDL, so `ava_runner` remains a CRUD-only runtime role
- **Wake-up mechanism**: Real-time agent wake-up via Redis pub/sub, replacing polling
- **Caching**: Debug-mode LLM response caching (Redis), reducing API calls
- **MCP protocol**: One supervised MCP daemon per machine, shared by agent tools
- **Service daemons**: Background processes such as agent-host, watchdog, heartbeat, labeler, memory-indexer
- **Observability**: LLM token usage logging

## Key Dependencies

- [[loop.ava.okf.md]] — The agent host owns database pools and inbound scheduling
- [[shared/lm/lm.ava.okf.md]] — The LLM invocation chain depends on the observe usage logging
- [[gateway-cli.ava.okf.md]] — The gateway depends on infra services (heartbeat, labeler)

## Entry Points

- `ava/_mcps_daemon.py` — shared per-machine MCP service
- `services/agent_host/daemon.py` — Agent host entry
- `services/watchdog/daemon.py` — Service liveness monitor entry

## Notes

- Postgres connection model: one host workload pool for turns/checkpoints and
  a separate control pool for admission, ownership and recovery. Idle agent
  identities do not retain private pools. The host's Redis subscription wakes
  work without PG LISTEN/NOTIFY.
- **Most** infra services are managed as sessions on the session backend, with watchdog health-checking every 60s and restarting on death (the list is derived from `build_services()` `ServiceSpec.healthcheck_module`). Two exceptions: ① permissions-helper is kept alive by launchd (KeepAlive plist, not in `build_services()` list, see [[services/agent_runner_side/permissions-helper.ava.okf.md]]); ② watchdog itself is unmonitored (`healthcheck_module=None`), if it dies, the user must manually `ava start`.
