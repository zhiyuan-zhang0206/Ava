---
type: doc
title: Chat inbound transaction ownership
description: Native caller-owned chat insertion and the existing standalone commit and wake wrapper
---

# Chat inbound transaction ownership

`insert_chat_inbound_in_transaction` accepts a connection already inside its
caller's transaction. It enforces the existing caller protocol, normalizes
caller payload/provenance, and writes the keyed chat identity and any central
audit/impersonation facts on that connection. It returns the existing
`ChatInboundReceipt` and an optional recorded telemetry event. It opens no pool
or transaction and performs no commit, live emission, wake or resurrection.
The caller may combine these facts with other domain writes, then commit or
roll back the whole operation. Emit the returned event and publish any wake
only after a successful commit. An idle connection, including autocommit outside
an explicit transaction, is refused before writing; implicit INSERT transaction
creation is insufficient for the caller-owned contract.

`insert_chat_inbound_once` remains the standalone compatibility owner. It opens
its existing transaction context, invokes the native writer, explicitly commits,
emits the recorded event and wakes only a newly inserted inbound, in that order.
`ChatInboundReceipt` return, source semantics, conflict detection and duplicate
handling remain unchanged. If post-commit telemetry or wake raises, it propagates
as `ChatInboundCommittedError` with that receipt, the original logical key and
chained cause. This error carries an observed commit, not a fabricated success
or retry decision.

Gateway `deliver_chat_inbound` and reconciliation await pending-row wake, live
`InboundArrived` publication and exact-row resurrection in their calling task.
Badge refresh, wake, live UI and resurrection remain separate effects. Known
Redis/network errors use the existing bounded best-effort policy; programming
errors propagate with the committed receipt. Automatic resurrection retains
known local refusals, machine pauses, verified remote refusals and unreachable
homes; an unknown RPC failure or malformed result propagates instead of being
logged as a successful request. Claimed/done rows only return the
receipt and observed status; they never revive stale work.

An HTTP post-commit error returns the existing 500 error envelope with
`retryable=false`, `committed=true`, `inbound_id` and the request's original
`idempotency_key`. A same-key retry or `/messages/reconcile` returns that same
row and repairs its pending tail without another chat/audit/resurrection effect.
A caller cancellation can lose its response after commit; it must reconcile
with the original key. Without a key, the receipt still proves commit but a new
request has no same-operation identity. No notification registry, UI outbox or
new durable custody mechanism is introduced. Publication adds its bounded
transport latency to the current request.

The completion digest uses this same awaited path as a heartbeat-service caller,
not an HTTP request. Unknown errors reach that service's existing `TaskGroup`;
the stable digest key reconciles a later service restart before event rows are
marked. The accepted boundary is recorded in
[Explicit runtime ownership boundaries](https://github.com/zhiyuan-zhang0206/Ava/blob/14a32f8a362ac6cc55b277c49e48e86f39d06347/docs/decisions/engineering/design/simplification/2026-10-09-explicit-runtime-ownership-boundaries.md).

`client_message_id` still lives on the inbound row: identical body/source/target
replays recover the original id, changed immutable identity raises
`ClientMessageConflictError`, and distinct or absent keys retain their existing
meaning. This refactor adds no receipt table or permanent tombstone; deleting
that row still removes this identity's evidence. The primitive is not a provider
source admission or execution guarantee. It changes no Weixin seen-set, routing,
command uncertainty or cursor behavior.

Owners: [[base/agents/messages/docs/caller_protocol.ava.okf.md]] and
[[base/agents/messages/docs/inbound-provenance.ava.okf.md]].

## Retry consumers

`delivery.retry.retryable_response` owns the gateway policy shared by SDK
transport and SDK/CLI outbox interception: 429/502/503/504 remain eligible for
bounded recovery. A structured `retryable=false` or `committed=true` refuses
automatic replay, and HTTP 500 exposes the original response once. Its durable
receipt and logical key remain available on `HTTPStatusError.response`.
Logical-key construction errors stop before sending instead of producing an
unkeyed message. Only named network failures enter transport/outbox recovery.
A terminal wire failure also retires any existing automatic recovery record
for that key. `retire_send(completed=False)` retains the sender's logical key
within its existing dedup window, so an explicit caller retry can recover the
receipt; only a completed send retires that key. No new journal state is added.

Outbox flush handles database connection failures (plain OperationalError
without SQLSTATE, class 08 errors, or PoolTimeout) through its existing backoff
and budget. Other errors propagate to the ops service with the journal intact.
A commit followed by an unknown wake error remains committed: explicit replay
with the stored key recovers that inbound without repeating its body effects.
