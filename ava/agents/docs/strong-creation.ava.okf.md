---
type: doc
title: Explicit Strong Agent Creation
description: Opt-in plain creation with fixed principal-scoped identity and no legacy fallback.
---

# Explicit Strong Agent Creation

`ava.agents.spawn(..., idempotency_key="intent", require_idempotency=True)`
opts into guarded plain creation. Keep the same caller key and effective inputs
when the response is lost. Strong mode requires an explicit 1–128 character key
and rejects `fork_from` before HTTP. The flag must be a boolean. The fleet
plugin's `label` wrapper forwards the same contract.

## Fixed admission

Strong calls always use `POST /api/keyed/v1/agents` with `Idempotency-Key` and
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
POST method, guarded logical path and raw key. Legacy and guarded paths have
different receipt namespaces: do not switch modes while retrying one intent.
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

A receipt proves birth acceptance and recovers the original agent id, not that
its native process is ready or work executed. Existing launch failure handling
and explicit `retry_launch(agent_id)` remain separate recovery operations.

## Compatibility and remaining scope

The default `require_idempotency=False` keeps `/api/agents` and its existing
headers, key behavior and retry gate. It does not promise recovery when an old
server ignores the key. Existing scripts, schedules and task compound creation
are not silently upgraded. `create_and_assign` has a separate explicit
[[gateway/agents/task_assignment/docs/task-assignment.ava.okf.md|guarded compound acceptance]]
mode with its own receipt; standalone spawn keys do not make a script atomic. Fork strong admission
and automatic ambiguous retry activation remain separate work.
