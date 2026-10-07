---
type: doc
title: Agent Startup
description: Host process initialization and per-agent runtime admission.
tags: []
---

# Agent Startup

The host process starts once per runner. It initializes process-wide logging,
schema/config checks and cluster extensions through `agent/process_boot.py`,
opens separate workload and control pools, builds the shared graph/checkpointer,
and reconciles stale local running rows before serving wakes.

A scheduled turn reads the agent's stored configuration and admits its exact
incarnation through `agent/ownership/hosted.py`. Per-agent identity is context-bound and the framework and plugin config travel
as the agent's slices, resolved before `boot_agent_scope()` builds the model and
restores state. The effective order is explicit overlay, then
birth config, then current cluster config.

`agent/startup/__init__.py` provides the shared recovery and saver operations:

- Reconcile claimed inbounds against the actual checkpoint.
- Repair crash-left unpaired tool uses/results.
- Wrap saver writes with visible failures and the configured N-step interval.
  Delta-bearing threads retire the interval: every super-step and write batch
  persists as upstream wrote it. The final flush removes its pending tail only
  after successful persistence.
- Reconcile retained pages and report desktop permission faults.

A missing or terminated row is not scheduled as normal work. A fresh foreign
runtime owner refuses admission. Maintenance holds also refuse ordinary work;
only the accepted control path can complete its own drain.

## Checkpoint interval

`AVA_CHECKPOINT_INTERVAL` defaults to four; the agent's `checkpoint_interval`
configuration can override it. The effective interval travels with the turn's
invoke configuration, so a shared host saver does not impose one agent's
interval on another. Ordinary threads can replay up to N-1 skipped super-steps
after a crash, including model costs and tool effects; terminal flush persists
the remaining tail. Delta-bearing threads retire throttling and retain every
super-step. Interval one restores every-step persistence. The operator
[canary protocol](../../../docs/conventions/agents/checkpoint-interval-canary.md)
owns verification and rollback.

## Related contracts

- [[admission.ava.okf.md]] — runtime ownership and admission
- [[../../docs/loop.ava.okf.md]] — host turn loop
- [[../../docs/state.ava.okf.md]] — checkpoint persistence
- [[page-restore.ava.okf.md]] — page reconciliation
