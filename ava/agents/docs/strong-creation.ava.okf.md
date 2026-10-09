---
type: doc
title: Keyed Agent Creation
description: Required caller-owned identity and immutable recovery for agent creation and forks.
---

# Keyed Agent Creation

`ava.agents.spawn(..., idempotency_key="intent")` requires an explicit
1–128 character key for creation or fork. Reuse the same key, effective inputs
and verified principal to recover the original agent after a lost response.
Use a new key for a deliberate new agent. The Fleet `label` wrapper follows the
same contract. Missing or invalid keys and the retired `require_idempotency`
flag fail before HTTP.

A fork resolves the source checkpoint during its first acceptance and copies
that chain, the fork marker and its first prompt in the birth transaction.
The immutable receipt commits in that transaction too. Replay returns that
child before mutable source/config/checkpoint validation: a later source
checkpoint, removed source checkpoint or changed defaults cannot replace it.

## Fixed admission

All calls use `POST /api/keyed/v1/agents` with `Idempotency-Key` and
`Idempotency-Scope: principal-v1`. Every connection retry and caller retry uses
that path, key and body. There is no capability GET, cached generation selection
or fallback to `/api/agents`, including after 404/405, 409, 422, 5xx or an
uncertain transport outcome. Older routing rejects the guarded path without
creating an agent. A subsequent request reaching an older router after a
successful birth also fails there; it cannot create a legacy replacement.

The transport retains the route owner's conservative retry gate. Connection
failures known to occur before sending may retry. A lost read/write response
remains terminal to the invocation; the caller can retry the same intent
explicitly. This change does not activate automatic ambiguous retries.

## Scope and result

The server records birth identity using the verified authorization principal,
POST method, guarded logical path and raw key. Other operation paths have different receipt namespaces; keep the same path
while recovering one intent.
A changed body within the same namespace returns 409. A different verified
principal identifies a different namespace, not the old receipt. SDK provenance
labels such as `spawner` do not choose the authorization principal.

Existing HTTP bearer and browser-session credentials authorize the same cluster
administrator principal. Different credential bytes therefore do not inherently
identify distinct operations. Authentication is still checked on every replay;
a revoked credential cannot retrieve the accepted result.

Maintain effective inputs across retries, including config/preset request fields
and any overlay recomputed by the caller: omitted `machine` resolves to the
caller's own machine, and the established actor supplies `spawner` and prompt
source. Changing context can change those fields even when the raw Python
arguments look the same. Within the same verified principal and key, such a
body change conflicts; it does not silently create another agent.

A current server records an immutable birth and original launch attempt. The
same intent may recover that original unadmitted attempt; after an explicit
retry rotates it, placement changes, admission, termination or deletion, replay
returns the historical agent ID without launching later work. A previously
guarded key with missing required snapshot returns 409 rather than recovering
from mutable metadata. Reusing the key cannot reset later operator config.

A receipt proves birth acceptance and recovers the original agent id, not that
its native process is ready or work executed. Existing launch failure handling
and observed-attempt `retry_launch` remain separate recovery operations; see
[[launch-retry.ava.okf.md]].

## Compound operations

`ava.tasks.create_and_assign` has its own required operation key and atomic
[[gateway/agents/task_assignment/docs/task-assignment.ava.okf.md|compound acceptance]].
A standalone spawn key does not make an entire script or workflow atomic.
The SDK has one creation path and does not select behavior using a mode flag.
