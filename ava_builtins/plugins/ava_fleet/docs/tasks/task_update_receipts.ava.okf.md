---
type: doc
title: SDK Task Update Receipts
description: Explicit agent-scoped operation keys deduplicate task update/log business effects in their existing database transaction.
tags:
- tasks
- sdk
- idempotency
---

# SDK Task Update Receipts

`ava.tasks.update(..., operation_key=...)` and
`ava.tasks.log(task_id, message, operation_key=...)` require a stable
caller key. Persist or retain that key for deliberate retries of the same logical
operation; do not replay an entire `execute_code` block to retry one mutation.
Missing keys and absent agent identity fail before mutation. The result remains `None`: no new task-state or execution result
is inferred from a successful replay.

## Scope and identity

The scope is `(actor_agent_id, task_id, operation_key)`. Raw keys must be strings
of 1 to 128 characters. Different actors or target tasks have independent scopes.
`log(message)` and the equivalent `update(note=message)` share one operation.

The actor comes from `ava.sdk_surface.agent_identity`, with a keyed call requiring
an established agent: validated borrowed external lease, hosted turn context,
then bound process context. It is not a caller-supplied actor argument. A native
turn, its launched script and an active external controller borrowing that same
agent share the agent's logical operation namespace across turns and attachments.
The existing borrowed-identity owner validates active lease, machine, controller
process tree and state version before accepting that identity. Replay validates
identity too; a revoked attachment cannot bypass its lease check with a receipt.

This is existing local execution provenance, not the gateway's authenticated
`principal-v1`. It adds no authorization to a process already holding direct DB
access, no runtime-incarnation fence, and no transaction-scoped lease authority.
Tool-call IDs, turn IDs, runtime generations and external tool labels are not
operation identities.

## Immutable inputs and commit

The immutable request contains effective validated inputs, before appending a
note timestamp or resolving a null parent into the current root id. Omitted,
`_UNSET` and null scalar values mean unchanged under the existing SDK contract.
`parent_id=None` remains an explicit move under the root, and `note=""` remains
a real append. The same scope/key with different effective fields raises
`ValueError` identifying a different task update.

`task_registry.update` takes the receipt advisory transaction lock before mutable
parent/task checks. After waiting, it revalidates the established actor, including
a borrowed lease; changed or invalid identity aborts the operation. A fresh write
uses the existing task row, audit and notification owners. It inserts its receipt
in that same `ava.DB.transaction()`. A producer failure rolls back the task,
appended note, task audit facts, notification intents and receipt together.

A retained `task_update_receipts` row proves that the void-returning database
mutation committed. Replay returns before changing the task, producing task
notifications or audit facts, or publishing a task update event. It cannot reset
a later reminder window or overwrite an intervening owner. General SDK call
metering may still record each invocation attempt; that observation is distinct
from another committed task effect. Notification processing remains owned by the
existing inbound queue and does not become an execution guarantee.

Receipts have no task/agent foreign key and no automatic expiry. Deleting a task
cannot let its old accepted key append again or pin cleanup. Any future receipt
retention requires an explicit replay horizon before removing tombstones.

## Remaining operations

Standalone `create` has its own provenance-scoped receipt owner in
[[task_creation_receipts.ava.okf.md]]. Compound `create_and_assign` has a separate
[[gateway/agents/task_assignment/docs/task-assignment.ava.okf.md|atomic acceptance]]
contract. Gateway task PATCH has its own actual-path and
credential-scoped receipt owner rather than sharing the SDK namespace.
