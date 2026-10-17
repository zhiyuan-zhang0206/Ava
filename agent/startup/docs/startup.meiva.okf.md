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

- Consume interrupted claimed chats, distinguishing committed history from unknown work.
- Pair crash-left tool uses with explicit unknown results without executing them.
- Wrap saver writes with visible failures and the configured N-step interval.
  Delta-bearing threads retire the interval: every super-step and write batch
  persists as upstream wrote it. The final flush removes its pending tail only
  after successful persistence.
- Reconcile retained pages and report desktop permission faults.

A missing or terminated row is not scheduled as normal work. A fresh foreign
runtime owner refuses admission. Maintenance holds also refuse ordinary work;
only the accepted control path can complete its own drain.

## Boot scopes

Process scope runs once per daemon. `init_process_scope()` starts the OTLP trace
init — only the cheap disk guards and the collector preflight run inline, the
heavy traceloop import proceeds on a daemon thread, and `turn_span` waits for it
before the first turn. `land_cluster_extensions()` lands the cluster's installed
skills on this machine so a machine that missed an install catches up on its next
start; failures are logged, not raised, and a cluster with no installed
extensions is a single indexed query returning no rows.

Agent scope runs once per agent, cached on the agent's stored configuration.
`boot_agent_scope()` creates the agent's workspace, seeds an empty `to_user.md`
when the file is missing, sends the one-time desktop-permission notice, and
builds the chat model for the agent's model, reasoning effort and thinking
budget. The workspace pre-create is unconditional — the folder is the
relative-path base for `meiva.files` / `meiva.shell.run` even when the prompt section
advertising it is off (bench runners) — and the seeded `to_user.md` is the
shared-context panel the user reads beside the agent's messages: it exists from
the agent's first boot, an existing file is never rewritten or re-stamped, and
the panel shows its empty state until the agent writes. The per-agent
`sdk_disable` list applies additively on the installed SDK surface (the install
already applied the env baseline; re-listing it is a no-op). The eval boundary is
added after plugins register: `meiva.memory` rebinds to the workspace's own
`memory-pool` with empty search results, and `agents.get_last_message`, `tasks`,
`mcps`, `ui` plus `web` / `understand` outside the allowlist are disabled.

Building the model eagerly is safe while the trace init is still in flight:
traceloop's LangChain wrap injects its callback handler at configuration time, so
a model built before the init completes still produces spans on its first call —
which happens inside a turn, after `turn_span` waited. The module keeps the chat
model and `.startup` imports function-level and `boot_agent_scope`'s return
annotation `Any`: the disposable exec child imports this module for the SDK
helpers and must not enter the LM stack (task #3585).

## Checkpoint interval

`MEIVA_CHECKPOINT_INTERVAL` defaults to four; the agent's `checkpoint_interval`
configuration can override it. The effective interval travels with the turn's
invoke configuration, so a shared host saver does not impose one agent's
interval on another. A crash can lose up to N-1 skipped super-steps; the host
starts at claim with fresh input rather than replaying that tail. Terminal flush
persists the remaining tail when it succeeds. Delta-bearing threads retain every
super-step. Interval one restores every-step persistence. The operator
[canary protocol](../../../docs/conventions/agents/checkpoint-interval-canary.md)
owns verification and rollback.

## Related contracts

- [[admission.meiva.okf.md]] — runtime ownership and admission
- [[../../docs/loop.meiva.okf.md]] — host turn loop
- [[../../docs/state.meiva.okf.md]] — checkpoint persistence
- [[page-restore.meiva.okf.md]] — page reconciliation
