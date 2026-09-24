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

The pending scan admits new recovery turns through two gates:
`AVA_HOST_RECOVERY_WAKE_BATCH` limits starts per scan and
`AVA_HOST_RECOVERY_WAKE_INFLIGHT` limits scan-started recovery turns still in
flight (both default to 4). Only a recovery turn started by that scan spends an
in-flight slot. Direct pub/sub wakes, force-cancel re-wakes with no task, and
turn-level stale-cancellation re-wakes do not spend one. Ordinary work, including
impersonation, and held maintenance wakes are exempt. The slot is released when
that recovery turn's own task ends. Only a replacement started by the pre-start
reaper from the same unconsumed wake inherits its slot. Other same-agent
successors, including a direct wake that starts during cancellation unwind,
run without occupying that slot.
Scan reconciliation waits for a queued pre-start reap to settle before releasing
its slot.

`AgentHost._invoke_until_done()` invokes the same checkpoint thread until idle
or a native lifecycle command ends the turn. Each invocation has its own trace.
Normal return flushes the final checkpoint before lifecycle application; a
failed flush cannot acknowledge a maintenance drain.

The final durable checkpoint is linked to the still-current turn trace, including
when N-step buffering delayed its write. Node-exit aggregates flush at invocation
return. Expected provider or compaction failures persist halted state and report
an error without discarding conversation history.

Database connection loss keeps the original single-flight task waiting with
bounded, cancellable backoff and a total ladder budget
(`AVA_HOST_DB_RECOVERY_BUDGET_SECONDS`): when the budget is spent the turn exits
through the crash path and the next wake retries.
Recovery revalidates the exact incarnation, flushes
retained writes, reconciles claimed input and repairs dangling tool pairs before
continuing — each stage under its own 30s `database_phase` bound (issue #1972),
never one aggregate deadline across the chain, so a healthy stage is not starved
by the combined time of the stages before it. It creates no inbound, model call
or maintenance acknowledgement; ownership loss stops the old continuation.
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
turn drops its runtime so the next admission re-runs reconciliation; an aborted
turn (expected provider/compaction failure) keeps its runtime and reconciles its
claimed inbounds at the settlement boundary itself
(`host_abort_reconcile_enabled`), so no row waits for a boot that may not come.
A turn that dies again under its own crash mark has spent its grace: the
settlement boundary terminates that corpse on the spot with the reaper's own
termination and events (`hosted_recrash_prompt_reap_enabled`), instead of
letting a zombie keep claiming and re-dying while the window runs out. Every
reaper termination also commits the death's recovery wake — one marked
system chat plus the guarded resurrect attempt
(`hosted_crash_recovery_wake_enabled`) — so a crash death with no arriving
work resumes near-field instead of waiting for the next scheduled wake
(task #4039).

## Entry points

Hosted admission records a coarse, durable `last_admission_outcome` and
`last_admission_at` on the agent row. Refusals distinguish maintenance hold,
publication deferral, resource evidence, and an unresolved guarded-update
refusal; the guard code never invents which predicate failed. The observation
is stamped only if the row still matches the attempted status and no later
admission superseded it. Successful admission stamps `admitted` in the same
transaction as the owner/lease update. These observations explain an unstarted
turn; they do not change wake, claim, or recovery behavior.

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
