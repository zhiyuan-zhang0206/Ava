---
type: doc
title: Guarded Agent Creation
description: Principal-bound creation admission with immutable birth receipts and fixed retry identity.
tags:
- gateway
- agents
---

# Guarded Agent Creation

`POST /api/keyed/v1/agents` accepts plain creation through the existing birth
transaction and launch owner. Every request requires a valid `Idempotency-Key`
(1–128 characters), the exact `Idempotency-Scope: principal-v1` header, and a
verified authenticated principal. Missing or invalid admission, an unverified
principal, and `fork_from` are rejected before birth or launch effects. Existing
authentication still applies to replay; revoked credentials cannot retrieve a
receipt.

The receipt identity includes the principal, POST method, and this versioned
logical path. Identical keyed requests replay the committed birth; changed
request data returns 409. The birth owner commits an immutable
`agent_creation_snapshots` record with the agent row, first prompt and audit
event in one transaction. Its manifest retains
the original agent ID, machine, config overlay, birth config and launch attempt
UUID. The request digest still uses the existing parsed `SpawnAgentRequest`;
this does not change the namespace or adopt guarded drafts' raw semantic identity.
Acceptance does not prove native execution.

Replay reads this retained birth before mutable placement, preset and model
checks. Only the original attempt, still idling, never admitted and on its
original machine, can receive the existing launch wake. A deliberate launch
retry, changed placement, admission, termination or deleted metadata returns
the original historical acceptance without forwarding a launch. Replay never
rotates an attempt, restores operator config or creates another first prompt.
The retained snapshot has no target FK or automatic expiry.

A guarded key created before snapshots were recorded fails with 409 when its
metadata still identifies the key but the snapshot is unavailable. Current
mutable metadata cannot prove the original attempt, so it is not backfilled or
used as a launch fallback. Already deleted unsnapshotted historical identities
remain unrecoverable; this change cannot reconstruct them.

An older gateway has no route for this versioned POST and rejects it without
creating an agent. A future strong client must keep the same path, key, and
body for every attempt of one intent. It must never downgrade that intent to
`POST /api/agents`: the legacy and guarded paths have distinct receipt
namespaces, and an older gateway may ignore a key on the legacy path. A cached
capability GET or observed generation cannot prove the backend serving a later
write supports keyed admission.

The SDK can explicitly opt in with `require_idempotency=True` and a caller key;
see the SDK owner at `ava/agents/docs/strong-creation.ava.okf.md`. It keeps the
same guarded path, scope and key for every attempt without capability discovery
or fallback. The default SDK helper still uses its existing route; removing
that compatibility branch is tracked in #4473. Gateway MCP creation now has only
`spawn_agent_guarded_v1`; the former `spawn_agent` tool is removed. Old installed
consumers are not an adoption gate. Automatic ambiguous retries remain disabled.

A previous gateway that already exposes this versioned path can still use its
older mutable launch projection. The path proves keyed admission support, not a
cluster-wide immutable recovery capability. Operators must stop or drain those
gateway generations before relying on original-attempt recovery; this change
does not perform that rollout. Other HTTP and compound-creation entry cleanup remains tracked in #4473.

Gateway MCP clients use `spawn_agent_guarded_v1` for retained original
birth recovery. The versioned tool name admits each write on its serving
gateway before effects and has a distinct authenticated MCP-client namespace;
unsupported servers reject it without legacy fallback. See the
[MCP endpoint owner](../../mcp_server/docs/mcp-endpoint.ava.okf.md).
