---
type: doc
title: Agent Runtime
description: Hosted agent turns with durable identity and isolated code execution.
tags: []
---

# Agent Runtime

One `services.agent_host.daemon` runs each machine's local agents as asyncio
tasks. Agent identity, config, model state and checkpoint threads stay separate;
an idle agent has no running task. There is one runtime architecture and no
per-agent process launcher or runner-mode selector.

The graph is the eight-node self-loop `after_init -> init_context -> claim ->
before_llm -> llm -> before_exec -> exec -> after_exec`. Claim returns to init
context for compaction and to END for idle or lifecycle control. Routing uses
`Command(goto=...)`; plugins extend the hook containers.

## Responsibilities

- `services/agent_host/dispatcher.py` multiplexes Redis wake events, limits turn
  concurrency and preserves single-flight per agent; durable pending work
  supplies the backstop when a pub/sub event is missed.
- `services/agent_host/host.py` admits the exact runtime incarnation, binds
  per-agent context/config, reuses model state, drives graph invocations, flushes
  checkpoints and settles lifecycle state before releasing the turn.
- `services/agent_host/db_recovery.py` retains the original turn during a database
  outage and revalidates ownership before repairing and continuing its checkpoint.
- `agent/graph/_llm.py` streams model inference with retry and cancellation.
- `agent/graph/_exec.py` runs `execute_code` in a disposable subprocess with an
  owned POSIX process group or Windows Job Object. Cleanup reaps its child and
  joins the output reader; this isolation is independent of host scheduling.
- Persistent shell sessions run in their own PTY hosts and survive normal agent
  restart and cluster pause. Full cluster stop closes them.

## Related contracts

- [[graph.ava.okf.md]] — graph topology and hooks
- [[state.ava.okf.md]] — state and checkpoints
- [[loop.ava.okf.md]] — turn scheduling
- [[lifecycle.ava.okf.md]] — native restart, terminate and resurrection
- [[context.ava.okf.md]] — per-turn dependency injection
- [[tool-calls.ava.okf.md]] — isolated code execution

Agents are allocated through `POST /api/agents`; the gateway commits their row
and work, then the home runner wakes its agent host. No agent Python process is
started directly.
