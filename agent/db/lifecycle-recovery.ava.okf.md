---
type: doc
title: Durable Lifecycle Recovery
description: Fixed command targets, process exit evidence, safe hosted settlement and bounded user-wake recovery.
tags: []
---

# Durable Lifecycle Recovery

## Completion evidence

Process lifecycle application fixes server-reserved `target_process_identity`
in the command under the admitted owner/target lock. Its PID is the actual
Python runtime, with OS birth evidence, not a DB timestamp or Windows
redirector. Controller and explicit resurrection share termination observation:
exact process disappearance/reuse permits completion and pointer clearing;
live, unreadable or missing historical evidence defers. Swept session records
do not erase this evidence.

Cold lifecycle acceptance revalidates the original applied command's exact
process evidence under the fixed target lock. A cold terminate can apply and
observe in that transaction because the entity is already proven absent; a
NULL PID, argv-only probe or expired lease cannot establish this. Missing
historical evidence remains pending. Hosted terminate applies and observes
atomically after the real single-flight continuation returns and its cache is
dropped; cache loss alone never proves quiescence.

## Force and later commands

Explicit force termination supersedes only earlier unfinished lifecycle
commands for the same agent and clears the matching active pointer under the
existing force fence. Original targets and applied timestamps remain audit
facts; superseded is not observed exit. Later commands and ordinary chat are
not acknowledged. Delayed old boots fail the actual command admission gate,
and a later explicit restart still requires proven exit and settlement of the
force. Already-running external effects cannot be retroactively undone.

## Attempt confirmation and user wakes

Launch confirmation follows the exact boot-attempt record returned by the
launcher, including off-path spawn confirmation. It does not require canonical
publication before admission; missing attempt evidence retains the hard boot
deadline rather than guessing a dead process from a missing canonical name.

A queued user wake arriving during old-process exit keeps its original inbound
identity. The existing bounded resurrection caller persists a reserved
blocked-terminate id and preparation-attempt count; it derives the fixed target
from that original command and revalidates it under the actual preparation
lock. Deadline is anchored to inbound creation, not HTTP redispatch. Exit-wait
attempts do not expand launch budgets. Exhaustion or changed ownership leaves
pending work with an explicit unresolved error, never a fabricated completion;
no background scanner is added. Gateway wake selection and runner admission
share the cluster-pinned boot grace value, retained in both config projections.
