---
type: doc
title: Task creation receipts
description: Standalone SDK creation returns an immutable accepted Task snapshot
---

# Standalone SDK task creation receipts

`ava.tasks.create(..., operation_key="...")` accepts a caller-chosen key of
1–128 characters. Retain the same key and inputs when the response is lost.
Keyless calls preserve their existing behavior. This contract covers standalone
creation; `create_and_assign` includes agent spawn and does not accept a key.
It does not protect retries of an entire `execute_code` script.

## Operation identity and request

A creation is scoped by canonical SDK actor agent id and the raw operation key,
independently of SDK task-update keys and HTTP credential namespaces. There is
no pre-existing target task id. An existing borrowed identity is validated before
admission and again after the receipt-key lock wait; an expired lease cannot
replay or create. This uses established execution provenance, not a new security
boundary or transaction-scoped lease fence.

The immutable request contains coerced title, description, required parent id,
priority, effective owner and requested reminder interval. `owner=None` and an
explicit owner equal to the actor are equivalent. A reminder interval of `None`
means use the priority default on first creation, and remains distinct from an
explicit interval even if the resulting seconds initially match. Changes to the
default policy do not prevent replay of that original request. Reusing a key
with a different request raises `ValueError`.

## Transaction and original result

The SDK transaction takes a creation-key advisory lock, checks a retained
receipt, then uses the existing parent lock, open-title uniqueness checks and
task writer for fresh creation. Task insert, creation audit, owner notification
intent and immutable receipt commit together. Failure rolls them all back.
Keyless creation retains parent and title checks without a receipt.
The task writer and shared snapshot model are native
[[base/agents/tasks/docs/creation-transactions.ava.okf.md|creation transaction primitives]];
the SDK remains the receipt admission and post-commit owner.

A receipt stores the complete originally returned `Task` dataclass snapshot,
including its rendered timestamp strings. Replay validates all fields and the
canonical status/priority values without filling missing defaults. It returns a
new `Task` object containing that snapshot, without reading or rewriting the
current task, parent, ownership, notification queue or reminder state. It does
not emit another business audit, notification or task-board event. SDK attempt
metering still observes every invocation.

This result proves the original creation was accepted, not that the task is
currently open or still exists. Use `ava.tasks.get(task.id)` for current state.
Renaming, closing or deleting the original task, deleting its parent, or reusing
its old title cannot make replay create a replacement. Timestamp rendering is
not repeated if cluster timezone settings change.

## Retention and remaining scope

`task_creation_receipts` is owned by the standalone creation transaction. Its
primary key is `(actor_agent_id, operation_key)`; request and result are JSON
objects. It has no foreign keys to mutable task/agent rows and no automatic TTL.
Receipt retention must preserve tombstones: deleting a receipt silently enables
execution of the same operation again. This change introduces no cleanup owner.

Compound `create_and_assign` still needs durable identity across spawn and task
creation. A title hash or a new key on each retry cannot provide that contract.
