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

The host shares one compiled graph and logical `services/agent_host/pooled_checkpoint.py`
checkpointer. Each cursor leases its own pool connection and delegates pipeline,
transaction and cancellation cleanup to LangGraph's saver for that connection.
Unrelated agents can therefore read and write concurrently. The N-step wrapper
in `agent/startup.py` still serializes writes and flushes for the same thread.

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
  or checkpoint failure. A 5s exact-owner probe precedes repair stages with
  independent 30s budgets: flush retained checkpoint writes, reconcile claimed inputs, revalidate
  ownership, repair tool state, and validate ownership again. Retry logs identify
  the failed phase, exception type, SQLSTATE and elapsed time. This repairs the
  agent's checkpoint/inbound consistency; it does not mean PostgreSQL crashed.
- `services/agent_host/recovery_interrupt.py` checks external cancel/terminate
  intent during backoff, advancing one retry without claiming the command or
  interrupting a write. At most one optional query runs per control pool; other
  observers skip that check, preserving capacity for ownership and lifecycle.
  Queries spend the existing backoff budget and create no background tasks.
  Persistent checkpoint unavailability still prevents a completed durable pause.
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
