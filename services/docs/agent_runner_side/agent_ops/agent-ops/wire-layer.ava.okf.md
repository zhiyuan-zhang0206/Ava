---
type: doc
title: Agent-Ops — Strongly-Typed Wire Layer
description: The ops RPC contract surface (ops/rpc_schemas/__init__.py) — OpEnvelope/OpResponse envelopes, the OpKind literal, and the per-kind payload/result models the daemon validates before dispatch.
tags: []
---

# Agent-Ops — Strongly-Typed Wire Layer

`OpEnvelope` carries `{kind, payload, idempotency_key?}` and `OpResponse`
carries `{status, result}`. `ops/rpc_schemas/__init__.py` defines the current `OpKind`
vocabulary and per-kind models. The outbound RPC client validates that vocabulary
before machine lookup, key generation or network activity; the receiver validates
it before maintenance admission, worker dispatch or database dedupe. Unknown
kinds return a failed result, including retired updater requests with historical
idempotency keys. There is no updater/bootstrap wire mode or restricted child
proxy.

Agent creation wakes use only `spawn-launch-v2`. `LaunchAgentRequest` requires
the committed UUID `launch_attempt_id`; the runner validates the idling row's
attempt and placement before publishing a repeatable wake. Prompt, source and
label are committed by the gateway and are forbidden in the launch payload.
The retired `spawn-launch` kind has no handler or wire registration.

Agent launch and lifecycle operations run asynchronously. Blocking maintenance,
configuration, inventory and shell operations run through
`services/agent_runner/agent_ops/dispatch_sync.py:dispatch_sync` on the daemon's worker pool.
Each handler validates its request model and serializes its result model as JSON.
Configuration and inventory writes retain their shared read-modify-write lock.

`LifecyclePayload.trigger_inbound_id` and `trigger_inbound_kind` bind pending-work
resurrection to the durable trigger. Versioned lifecycle paths reject unknown or
retired actions rather than silently dropping their guards. `OpFailure` carries
`error`, `detail` and the optional `AvaAgentError` reason, allowing the gateway to
reconstruct the same business failure.

The shared envelope lives in `base/api_contracts/op_envelope.py`; per-operation
models live below gateway in `ops`. Supplied keys contain 1 to 128 characters.
Ops replay records retain a canonical SHA-256 of kind plus immutable payload.
A changed kind/payload, collision with another channel, or legacy record lacking
that identity returns a failed result without dispatch. JSON object key order
does not change identity. Legacy keys keep their namespace, but a lost historical
request identity cannot be recovered by guessing or issuing a fresh command.

Claims, effects and response writes are separate commits. Exceptions, cancellation
and result-write failures retain the pending claim; duplicates wait within a
bounded budget, then report an uncertain outcome that requires inspection. The
record is not a transactionally committed business receipt. Ops records are not
TTL-pruned or stolen by HTTP response-cache claims; retiring an old command must
wait for domain-owned recovery and expiry rules. Inspect request_hash, original
pending payload, op_status and completed_at in api_idempotency before recovery.
No change here authorizes redispatch with a new key after an uncertain effect.
Removed updater operations have no special duration or concurrency policy.

`OpStatus` in `ops/rpc_schemas/__init__.py` owns `completed` / `failed`,
independent of each operation's business result. Dispatch and closure-notice
receipts store those same strings; an idempotency replay converts its recorded
status before returning it. NULL still means the original owner has not
completed; an unknown stored terminal status is an error, never replayed as
success. The RPC client validates the response envelope before interpreting it.

`upload-receive-v1` is a naturally repeatable manifest-bound batch copy, with an
explicit integer version and actual native unit check before effects. Its own
receiving reservation and create-only files own recovery; no transport key/cache
or overwrite fallback is used. Ready retries verify actual objects. See
[[gateway/upload_delivery/docs/delivered-uploads/delivered-uploads.ava.okf.md]].
