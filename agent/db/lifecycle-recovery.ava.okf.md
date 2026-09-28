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

Resurrection requires the terminated hosted incarnation's retained generation
and owner, no per-agent PID, and no outstanding applied lifecycle command. A
never-admitted row (no runtime identity, birth marker unconsumed, rechecked in
the final CAS) resurrects as a fresh hosted birth instead.
Historical process runtimes, unknown runtime kinds, incomplete hosted
identities and resources the current model cannot decode refuse resurrection
(`runtime_cutover_required`) pending explicit one-time cutover reconciliation.
For a terminated row with NULL resources and an incomplete identity, that
reconciliation is the cutover's identity mint: on an attested machine it
writes a hosted kind and a fresh generation and owner, which this gate then
accepts and the resurrection CAS clears
(`decisions/2026-09-28-legacy-terminated-agents-resurrectable-at-cutover.md`).
Rows the mint does not reach keep refusing.
An automatic resurrection that meets such a refusal leaves its inbound queued
and logs a WARNING naming the reason.
No process-exit observer adopts those rows or settles a hosted logical lifecycle.

Hosted protocol-zero rows with NULL resource metadata retain their existing
settlement behavior. NULL is not a resource-closure proof; the broader resource
and database cutover remains required.

## Force and later commands

Explicit force fixes the original command and target, supersedes earlier
unfinished commands for that identity, and cancels the actual host task.
Superseded is not observed exit. Resource settlement still guards later
admission; already-started external effects cannot be undone by a DB write.
Later commands and chat are not acknowledged as a side effect of force.

## Resurrection epochs

Every resurrection records the id of its `kind='resurrect'` inbound as the
identity's epoch fence. Earlier unapplied restart/terminate commands were
superseded by an intent that came after them: the resurrection transaction
settles them as superseded (the payload names the resurrect inbound), and
acceptance settles any such row it still finds instead of adopting it. An
applied command is never rewritten, and no observation timestamp is invented.
A command created after the resurrect inbound is current intent and can still
stop the new incarnation.

## Resurrection and user wakes

`ops/agent_wake.py` locks home placement and the pause latch, verifies the
terminated state and outstanding lifecycle evidence, commits the epoch fence
with its resurrection marker and optional work, then publishes the wake. A
refusal rolls the whole transaction back, fence included. The agent host admits
the successor; no OS launcher is involved.

A queued wake keeps its original inbound identity. A changed home, paused
machine or unsettled prior execution cannot be bypassed by retrying the wake.

A corpse reap commits its own wake (task #4039): the terminating transaction
also queues one `hosted_turn_recovery`-marked chat, and the service layer
attempts the guarded resurrect right after — a crash death with no arriving
work therefore still resumes near-field, with the delivery watchdog's
terminated-owner retry as the backstop.

## Entry Points

- `agent/hosted_ownership.py` — native completion and resource settlement
- `agent/lifecycle_observe.py` — successor admission observation
- `ops/agent_wake.py` — transactional resurrection
