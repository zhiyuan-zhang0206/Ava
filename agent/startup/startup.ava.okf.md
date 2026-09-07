---
type: doc
title: Agent Startup
description: Host process initialization and per-agent runtime admission.
tags: []
---

# Agent Startup

The host process starts once per runner. It initializes process-wide logging,
schema/config checks and cluster extensions through `agent/_process_boot.py`,
opens separate workload and control pools, builds the shared graph/checkpointer,
and reconciles stale local running rows before serving wakes.

A scheduled turn reads the agent's stored configuration and admits its exact
incarnation through `agent/hosted_ownership.py`. Per-agent identity, framework
config and plugin config are context-bound before `boot_agent_scope()` builds
the model and restores state. The effective order is explicit overlay, then
birth config, then current cluster config.

`agent/startup.py` provides the shared recovery and saver operations:

- Reconcile claimed inbounds against the actual checkpoint.
- Repair crash-left unpaired tool uses/results.
- Wrap saver writes with visible failures and the configured N-step interval.
  The final flush removes its pending tail only after successful persistence.
- Reconcile retained pages and report desktop permission faults.

A missing or terminated row is not scheduled as normal work. A fresh foreign
runtime owner refuses admission. Maintenance holds also refuse ordinary work;
only the accepted control path can complete its own drain.

## Related contracts

- [[admission.ava.okf.md]] — runtime ownership and admission
- [[../loop.ava.okf.md]] — host turn loop
- [[../state.ava.okf.md]] — checkpoint persistence
- [[../page-restore.ava.okf.md]] — page reconciliation
