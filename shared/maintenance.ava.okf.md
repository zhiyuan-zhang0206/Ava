---
type: doc
title: Explicit local maintenance
description: A durable home-local hold around existing restart, claim and checkpoint boundaries.
status: current
---

# Explicit local maintenance

`maintenance.py`, `maintenance_state.py` and `maintenance_cohort.py` extend the
existing [pause-owner journal](pause_owner.ava.okf.md). There is no new database
pause table or native graph hook. The operation is the exact `(holder,
acquired_at)` pair in `$AVA_HOME/run/deploy-pause-owner.json`; it has no TTL.
Ordinary startup, update, compensation and stranded-pause recovery cannot clear
it. Invalid/unreadable journal state refuses admission.

Preparation publishes the hold before locking native `agents_meta` rows. Hosted
admission checks it again after the same row lock. Existing native iterations
continue normally until an ordinary `restart` inbound reaches claim; lifecycle
priority preserves ordinary pending messages. A request just after claim can
allow another iteration before the next claim. No new LLM/exec/compaction pause
hook changes this behavior.

The journal records one restart ID per original live hosted incarnation.
Preparation first records the cohort, then commits commands, then records their
IDs. Repeating it after either file-write failure reuses the same commands.
Normal claim owns target generation/owner binding. Clean, unowned idle rows
without a PID, resources or unresolved claimed/lifecycle work are recorded as
parked intent and left unchanged. Stale foreign owners, process-mode runtimes,
external control leases and unknown work require separate resolution.

A host records drain only after the shielded continuation, final checkpoint
flush, resource closure and owner settlement have returned. The DB restart must
be applied, unobserved, and targeted to that same host boot. A successor cannot
sign an absent original receipt. Failures latch in memory **before** any journal
I/O and, where possible, in the journal; another wake cannot turn failure into
drain. `applied_at`, an idle row, or a released lease alone is insufficient.

Phases are `preparing → draining → drained → stopping → stopped → starting →
ready`. Only explicit same-operation resume releases admission. Failed prepare,
drain, stop or start retains its phase and hold; retries and `resume --cancel`
are explicit. Cancel restores ordinary recovery, and does not promise replay
safety for a failed arbitrary external effect. Successful resume wakes the
saved restart IDs; their existing DB pointers also survive a lost Redis wake.
Cold admission observes the pointer and reloads the checkpoint. Halted agents
remain idle; unfinished native work continues; terminated rows are not revived.

During prepare/drain, dependency APIs remain available. Explicit local stop
changes phase to `stopping`, closes new ops admission and waits for both request
handlers and actual executor futures before signalling services. The HTTP
server's socket close is insufficient when a client disconnects before its
handler finishes. This accounting does not cover detached jobs or OS services.

The CLI-only `authorized_start` ContextVar is an exact-operation capability for
nested startup/unpause calls in one process; it is not propagated to service
processes and never replaces the persistent journal authority.

See [the operator procedure](../conventions/graceful-maintenance.md) for the
local/fleet distinction, external resources and first-deployment limitation.
