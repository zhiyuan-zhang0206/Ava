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
row with no runtime identity resurrects as a fresh hosted birth instead, in
two cases, each rechecked in the final CAS. One is a never-admitted row whose
birth marker is still unconsumed. The other is a row this runtime
force-terminated while no incarnation owned it: the force records
`unowned_termination` on its own command, but only when the row's unowned
state has a lifecycle origin. That origin is the spawn's birth epoch
(`last_resurrect_inbound_id = 0`), or a `lifecycle_release` receipt on the
resurrection or applied restart that left the row unowned. The receipt must
belong to the current life, that is, have an id above the epoch fence
(`docs/decisions/2026-09-29-unowned-termination-resurrects.md`).
Historical process runtimes, unknown runtime kinds, incomplete hosted
identities and resources the current model cannot decode refuse resurrection
(`runtime_cutover_required`); no code reconciles them. A terminated row with
NULL resources passes this gate only with a minted hosted identity (a hosted
kind and a fresh generation and owner), which the resurrection CAS clears
(`docs/decisions/2026-09-28-legacy-terminated-agents-resurrectable-at-cutover.md`).
Every other such row keeps refusing, and so does a legacy unowned row this
runtime force-terminated without having released it first.
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

`ops/agents/wake.py` locks home placement and the pause latch, verifies the
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

- `agent/ownership/hosted.py` — native completion and resource settlement
- `agent/ownership/lifecycle_intent.py` — successor admission observation
- `ops/agents/wake.py` — transactional resurrection
