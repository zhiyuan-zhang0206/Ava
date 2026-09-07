---
type: doc
title: Durable Lifecycle Recovery
description: Exact incarnation targets, host settlement and durable successor observation.
tags: []
---

# Durable Lifecycle Recovery

## Completion evidence

Native restart and terminate target the generation and owner accepted under
the metadata lock. Claim returns END; the host flushes the checkpoint and
settles the actual continuation and managed resources before applying the
command. Cache eviction, a NULL PID, an expired lease or a health endpoint
alone cannot prove completion.

Restart keeps its applied command pointer for a successor admission to
observe in its own transaction. Termination is observed after the original
execution is settled. Cold recovery only completes a command when its
original owner and resources are positively absent; unresolved evidence
remains pending.

Historical process-target evidence stays readable in existing rows. It is
compatibility data for settlement, not authority to launch an agent process.

## Force and later commands

Explicit force fixes the original command and target, supersedes earlier
unfinished commands for that identity, and cancels the actual host task.
Superseded is not observed exit. Resource settlement still guards later
admission; already-started external effects cannot be undone by a DB write.
Later commands and chat are not acknowledged as a side effect of force.

## Resurrection and user wakes

`ops/agent_wake.py` locks home placement and the pause latch, verifies the
terminated state and outstanding lifecycle evidence, then commits idle intent,
its resurrection marker and optional work. Only after commit does it publish
the wake. The agent host admits the successor; no OS launcher is involved.

A queued wake keeps its original inbound identity. A changed home, paused
machine or unsettled prior execution cannot be bypassed by retrying the wake.

## Entry Points

- `agent/hosted_ownership.py` — native completion and resource settlement
- `agent/lifecycle_observe.py` — successor admission observation
- `ops/agent_wake.py` — transactional resurrection
- `shared/lifecycle_termination_observe.py` — prior termination evidence
