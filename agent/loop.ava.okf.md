---
type: doc
title: Agent Turn Loop
description: The agent host schedules and drives isolated graph turns.
tags: []
---

# Agent Turn Loop

`services/agent_host/daemon.py` owns the process; `AgentHost` owns local agent
turns. `TurnScheduler` serializes each agent while allowing bounded concurrency
between agents. A wake with no work creates no model call; idle ends the task.

`AgentHost._invoke_until_done()` invokes the same checkpoint thread until idle
or a native lifecycle command ends the turn. Each invocation has its own trace.
Normal return flushes the final checkpoint before lifecycle application; a
failed flush cannot acknowledge a maintenance drain.

The final durable checkpoint is linked to the still-current turn trace, including
when N-step buffering delayed its write. Node-exit aggregates flush at invocation
return. Expected provider or compaction failures persist halted state and report
an error without discarding conversation history.

Database connection loss keeps the original single-flight task waiting with
bounded, cancellable backoff. Recovery revalidates the exact incarnation, flushes
retained writes, reconciles claimed input and repairs dangling tool pairs before
continuing. It creates no inbound, model call or maintenance acknowledgement;
ownership loss stops the old continuation.
An abort awaiting its halted/breaker write remains pending across a database
outage; recovery retries that write instead of invoking the model again.
Database-only invocation boundaries have a retry deadline; it does not bound
model inference, code execution or the overall graceful drain. Recovery has
separate, expiring evidence tied to its original task and incarnation, so local
and gateway stall detectors can distinguish database waiting from a stuck turn
without changing the actual node-progress clock.

Runtime construction binds agent identity and both config layers before model
creation and startup reconciliation. Cache eviction removes model/runtime
objects, not agent identity, history or database ownership. A failed or cancelled
turn drops its runtime so the next admission re-runs reconciliation.

## Entry points

- `services/agent_host/daemon.py:run` — host startup, health and ownership renewal
- `services/agent_host/dispatcher.py:TurnScheduler` — wake scheduling and single-flight
- `services/agent_host/host.py:AgentHost.run_turn` — admission and settlement
- `services/agent_host/stall_guard.py:run_invocation_with_stall_guard` — shared invocation guard
- `services/agent_host/db_recovery.py:recover_database` — retain and recover an interrupted turn

## Related contracts

- [[startup/startup.ava.okf.md]] — host and per-agent initialization
- [[lease.ava.okf.md]] — incarnation ownership
- [[lifecycle.ava.okf.md]] — native control and checkpoint ordering
- [[sessions.ava.okf.md]] — persistent shells
