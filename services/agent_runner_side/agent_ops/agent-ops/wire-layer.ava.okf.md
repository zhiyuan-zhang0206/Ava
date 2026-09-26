---
type: doc
title: Agent-Ops — Strongly-Typed Wire Layer
description: The ops RPC contract surface (ops/rpc_schemas.py) — OpEnvelope/OpResponse envelopes, the OpKind literal, and the per-kind payload/result models the daemon validates before dispatch.
tags: []
---

# Agent-Ops — Strongly-Typed Wire Layer

`OpEnvelope` carries `{kind, payload, idempotency_key?}` and `OpResponse`
carries `{status, result}`. `ops/rpc_schemas.py` defines the current `OpKind`
vocabulary and per-kind models. The outbound RPC client validates that vocabulary
before machine lookup, key generation or network activity; the receiver validates
it before maintenance admission, worker dispatch or database dedupe. Unknown
kinds return a failed result, including retired updater requests with historical
idempotency keys. There is no updater/bootstrap wire mode or restricted child
proxy.

Agent launch and lifecycle operations run asynchronously. Blocking maintenance,
configuration, inventory and shell operations run through
`services/agent_ops/dispatch_sync.py:dispatch_sync` on the daemon's worker pool.
Each handler validates its request model and serializes its result model as JSON.
Configuration and inventory writes retain their shared read-modify-write lock.

`LifecyclePayload.trigger_inbound_id` and `trigger_inbound_kind` bind pending-work
resurrection to the durable trigger. Versioned lifecycle paths reject unknown or
retired actions rather than silently dropping their guards. `OpFailure` carries
`error`, `detail` and the optional `AvaAgentError` reason, allowing the gateway to
reconstruct the same business failure.

The shared envelope lives in `shared/api_contracts/op_envelope.py`; per-operation
models live below gateway in `ops`. Supported non-idempotent deliveries retain
one bounded database dedupe outcome per key; removed updater operations have no
special duration or concurrency policy.
