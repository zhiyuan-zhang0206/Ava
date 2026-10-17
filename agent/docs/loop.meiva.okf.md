---
type: doc
title: Agent Turn Loop
description: The agent host schedules and drives isolated graph turns.
tags: []
---

# Hosted agent loop

Each execution machine runs its own agent host. The shared host owns unique
agent admission and runs serialized turns. The LLM graph claims input, calls the
model, executes code in a local child process, then returns to claim. An idle
agent occupies no resident turn task. Filesystem and command execution remain
on the agent's home machine; no failure path relocates them.

A fresh wake starts halted at claim, so only new inbound or explicit control
can start work. A current healthy turn can continue ordinary graph steps until
idle or lifecycle exit. Every admitted invocation runs once. Database phases
have finite deadlines; failed result or checkpoint saving does not re-enter the
graph. Claimed interrupted chat is consumed with unknown outcome rather than
redelivered. Dangling tool calls are repaired with an `unknown` execution status.

Cancellation reaches the owned task. Known resource cleanup has a finite wait;
unresolved work retains its identity fence and requires process shutdown.
A host that fails database ownership renewal on three consecutive beats exits.
Crash reaping records termination and diagnostics without producing a recovery
chat. Watchdog force/resurrect and persistent terminated-owner retries are absent.

Ordinary new chat still wakes a terminated agent on its home machine. Normal
heartbeat is new input for a runnable agent. A permanent provider error stops
background wakes after one rejection; new chat or explicit operator action may
try after the cause is fixed. There is no billing recovery tool or SDK outbox.

Strong native controls retain exact-target acceptance and normal original
completion. Interrupted cancel/compact/restart can stay held for inspection;
a later generation never automatically reconstructs their successful receipts.

## Entry points

- `services/agent_runner/agent_host/daemon.py` — startup, health and finite DB renewal.
- `services/agent_runner/agent_host/dispatcher.py` — wake scheduling and single-flight.
- `services/agent_runner/agent_host/host.py` — admission, one-shot work and settlement.
- `services/agent_runner/agent_host/invocation/native_work.py` — fresh-input boundary.
- `services/agent_runner/agent_host/db_recovery.py` — database-only deadline.

## Related contracts

- [[agent/startup/docs/startup.meiva.okf.md]]
- [[agent/ownership/docs/ownership.meiva.okf.md]]
- [[lifecycle.meiva.okf.md]]
- [[base/agents/incarnation/docs/native-work-cancel.meiva.okf.md]]
