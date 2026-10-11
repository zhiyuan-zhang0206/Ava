---
type: doc
title: Task PATCH Operation Receipts
description: Immutable task PATCH response snapshots committed with task changes and notification intents.
tags:
- tasks
- idempotency
---

# Task PATCH Operation Receipts

`PATCH /api/tasks/{task_id}` requires `Idempotency-Key`. Missing keys fail
before mutation. Every accepted request commits the task
mutation, its existing notification intents and the original `TaskRow` response
in one transaction. A lost response can be replayed with the same key and body.
Acceptance does not prove that an agent has processed a notification.

## Identity and immutable input

The HTTP owner is `gateway.http.auth.request_principal.request_key`. The key
must arrive with `Idempotency-Scope: principal-v1` and is scoped by verified
principal, HTTP method and actual path. A caller key reused for another task
path or principal identifies a separate operation, rather than a conflict.

The immutable body is the validated `TaskUpdateRequest` JSON projection with
`exclude_unset=True`. Omitted fields remain distinct from explicit nulls,
including `parent_id=null` (move under the root). JSON property order does not
matter. Unknown input fields follow the existing Pydantic boundary behavior.
The same identity with a different projected body returns 409 before mutable
task validation or writes.

## Transaction and replay

`tasks._patch_task_blocking` takes the operation's advisory transaction lock
before reading `task_patch_receipts` or acquiring the existing task row lock.
A fresh request follows the existing task rules, reminder reset and notification
policy. Its immutable response is inserted before that same transaction commits.
Failure in task, notification or receipt production rolls back every effect.

A replay returns the original response snapshot. It does not reset reminder
counters, update timestamps, overwrite an intervening owner change, supersede
new assignment messages, enqueue notifications, emit new audit facts or wake
any agent. Mutable task state is not read to reconstruct the result. The existing
notification recovery owner handles original pending delivery independently.

Receipts have no task or inbound foreign key and no automatic expiry. They
remain tombstones after task or notification cleanup: removing their results
would let an old accepted operation execute again. Retention changes require a
separate replay-horizon contract. `TaskRow` schema evolution must keep stored
snapshots readable or add an explicit forward migration.

## Clients and remaining boundaries

The generic SDK `transport.patch` sends one stable key and the `principal-v1`
scope for a keyed route and retries an ambiguous outcome under that key;
callers may supply the original key for deliberate replay. Malformed supplied
keys fail before network I/O. Non-keyed routes keep their existing wire shape;
a supplied key does not make such a route retryable.

The browser currently reads tasks; the gateway MCP server has no task write
tool. Direct `ava.tasks.create/update/log` writes do not pass through this HTTP
receipt owner. Those SDK writes require actor-scoped operation keys; `create_and_assign` uses
one atomic compound receipt. These remain separate namespaces.
