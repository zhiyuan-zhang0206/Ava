---
type: doc
title: Guarded manual compaction
description: Planned native execution proof for one frozen manual compaction target.
tags: [agent, compaction, idempotency]
---

# Guarded manual compaction

## Scope

This is an implementation design, not an advertised capability. Baseline:
main `71f908170b403cfaeb4f8981ed93d28459fdd73d`; native work/transfer
interfaces are pending the native-cancel contribution. Do not copy that WIP.
Implement the complete acceptance-to-cold-proof chain before publishing a PR.

Prevent duplicate generation/application, lost accepted execution and overwriting
new history for an explicit guarded manual request. Initially require a proven
quiescent managed source: actual ended native work, closed owned resources and
the serialized host owner gate. `agents_meta.status='idling'` alone is insufficient.
Running targets, unmanaged consumers and unknown old executors fail closed.
Keep legacy HTTP/CLI/UI compact, automatic compact and agent-authored summaries
unchanged. No client outbox, generic worker or provider exactly-once promise.

## Prior art and existing evidence

`control_delivery.accept_control` already commits a keyed receipt with one inbound.
It deliberately promises acceptance, not execution. Generic claim marks non-chat
rows done before dispatch. Manual dispatch retries `generate_summary`, keeps the
result only in memory and replaces earlier payloads when several compact requests
share a batch. `_compact_outcome` publishes SUCCESS and finalizes chats before its
graph transition is flushed. `init_context` later materializes `context_reset`.
`mark_compact_boundary` currently stamps the newest checkpoint best-effort.

Hosted failure tests prove done + unchanged history + halted, not application.
The two-request test expects two LLM calls and only the second summary. Reuse
native work, certified transfers, shielded continuation and cold reads;
cancel-specific receiver helpers do not establish compact authority.

## Simplest sufficient design

Alternative A (selected): freeze an observed quiescent history and reject drift.
Alternative B: accept a request now and choose its history at a later claim;
this preserves more of legacy UX but does not act on the caller's observed target.

Add dedicated versioned observe/accept/status routes, provisionally
`/api/keyed/v1/agents/{id}/compact-target`, `/compact-history`, and
`/compactions/{command_id}`. Accept requires a caller key and verified principal-v1
scope using actual method/path. New protocol routes never downgrade to legacy.
Keep the guarded retry gate conservative until its complete execution contract
exists. No SDK/browser/MCP activation in this slice.

### Target and domain record

Observe requires a typed compact-specific source observation produced by the
actual new host inside its serialized quiescent boundary: producer protocol,
original ended work, closed-resource evidence and cold history anchor. Native
work protocol 1, admission envelope 3, a Gateway route or a managed/settled row
does not imply support for this executor. No deployment enable flag or guessed
binary capability. Bind the observation to its real producer generation/owner;
replacement invalidates fresh eligibility unless the exact transfer is certified.
Old runners cannot claim or ACK this domain protocol. Keep its pointer out of
generic inbound dispatch, and retain diagnostic intent without legacy fallback.

Strict `CompactTarget`: explicit integer protocol, positive non-bool agent ID,
source checkpoint ID/namespace, messages channel version, compact segment version,
and original ended-work/managed owner attribution. Reuse checkpoint/message and
native identity owners, not a parallel serializer. The source snapshot remains
retained. Administrative projections may create descendants without changing
messages/compact versions; a new chat, self/auto compact or other history write
makes the target stale. Never equate latest checkpoint ID with unchanged history.

One additive domain table retains scoped key, immutable request/acceptance,
command ID, original target, fixed generation attempt, result and checkpoint proof.
No cleanup FK or TTL. Replay precedes mutable eligibility and survives deletion.
Same key/different immutable request conflicts; credentials are rechecked.
Use bounded outcomes: accepted, prepared, applied, noop, rejected, uncertain.
Record safe reason separately; inbound done is never an outcome proof.

Fresh acceptance serializes with native owner/command admission, verifies the
quiescent source and admits one exclusive compact pointer. Another key cannot
race a pending compaction for that history. Observe is advisory; acceptance and
execution each revalidate under the owner. Empty history commits a frozen NOOP;
replay after later chat must remain that NOOP.

### Generation and application

1. Under the current exact receiver gate, durably claim the original generation
   attempt before the first call. Release DB connections across LLM work.
2. Use the existing summary/prompt owner once at application level. Unknown
   transport failure, cancellation or process death without a durable result
   becomes UNCERTAIN, not a retry. Provider/cache internals must be audited:
   their attempts are not externally exactly-once. Do not retain a blind
   `COMPACT_MAX_ATTEMPTS` loop for this new path.
3. Persist a typed prepared summary, digest and required message metadata with
   original attempt/receiver CAS before changing history. Lost result-commit
   response rereads this result. A late result may add original-attempt evidence
   but cannot reverse a terminal disposition or bypass owner/cancel fences.
4. Revalidate target history and competing commands, then use the existing
   transition builder. Bind command, original target, attempt, result digest and
   resulting segment version in a native graph marker. Preserve it through
   `init_context`; the parked summary is not yet materialized history proof.
5. Flush, then use a fresh cold reader to verify the complete applied state and
   latest-head/receiver authority. Commit APPLIED and exact checkpoint evidence.
   Lost ACK rereads the marker/result; it must not call the LLM or wipe again.

Finalize only original chats proved covered by this source/result, after durable
application proof; do not blanket-finalize unrelated claimed rows. Pending new
chats remain pending and resume normally after settlement. Stamp the frozen
source anchor, not an arbitrary newer checkpoint. Publish success/audit/metrics
after application proof through existing postcommit owners; hint loss is not
application failure. Retain the source snapshot for diagnostic/history reads.

### Cancellation, transfer and uncertainty

Compact dispatch is exclusive, not a generic co-chat batch. Reuse native host
serialization and resource certificates. Cancel/terminate/restart must arbitrate
through the same owner; no lock held during provider calls. A result prepared
before cancellation stays evidence but does not authorize application afterward.

A successor may finish PREPARED without another LLM call only with exact original
work/attempt transfer authority and unchanged history. Cold APPLIED proof settles
without replay. An unknown executor is not dead merely because its lease expired.

Generation UNCERTAIN can release later ordinary inputs only after actual executor
end/certified authority plus cold proof that no application marker exists. Retain
the diagnostic/result and never automatically generate again. Otherwise hold.
In application uncertainty, cold proof resolves applied versus unchanged source;
unproven or changed history stays held/diagnostic, never reconstructed by guessing.
Terminal replay cannot rewrite subsequent legitimate work.

Implementation gates and the actual consumer/fault matrix are recorded in
[the companion audit](guarded-manual-compaction-consumers.md).
