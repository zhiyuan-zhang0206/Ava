---
type: doc
description: "Observed source, one native generation attempt and cold application proof."
title: Guarded manual compaction
---

# Guarded manual compaction

This domain protects one explicit manual history replacement. Legacy `/compact`,
automatic compaction and SDK self-compaction retain their existing semantics.
The SDK offers explicit source observation, submission and retained status via
[[ava/agents/docs/compaction.ava.okf.md]]; default controls and UI/CLI/MCP
consumers remain separate. This does not authorize runtime rollout.

## Observation and acceptance

Only the new native host produces `CompactTarget(protocol=1)` after its actual
serialized pump task exits, all owned resources close, and its source work is
SETTLED with an actual `ended_at`. It reads a fresh persisted root checkpoint,
reconstructing delta-written messages without the runtime cache or pending task
writes. The source includes exact work identity, root checkpoint, message and
compact channel versions, segment version and model. A router, status string,
host-admission envelope or older native-work protocol cannot advertise support.

GET `/api/keyed/v1/agents/{id}/compact-target` returns this retained observation
only while its current managed receiver, closed-resource evidence, history and
input admission remain eligible. Administrative projections can change the
checkpoint ID; changing message/compact versions makes the source stale.

POST `/api/keyed/v1/agents/{id}/compact-history` requires this typed target,
`Idempotency-Key` and verified `Idempotency-Scope: principal-v1`. The existing
principal owner includes method, concrete path and authenticated namespace.
Same key/body replays the original acceptance before mutable eligibility;
changed body in that scope conflicts. Other principals/paths are independent.
202 means durable acceptance, never application or provider completion.
The receipt and one unresolved command pointer commit together; generic work
preparation and inbound claim cannot bypass that pointer. The existing retained
wake scan revisits pending commands; there is no separate worker or outbox.

Observation/receipt tables have no cleanup FK or TTL. Deleting mutable source
records does not authorize another effect under an old key. Retained diagnostic
records require an explicit future retention owner, not implicit queue cleanup.

## Source and input ordering

No inbound ID is a commit watermark. Actual inbound INSERTs run the
`inbound_messages_impersonation_history` AFTER INSERT trigger, which takes
`agents_meta FOR UPDATE` until commit. Compact admission, attempt claim and
application authorization use that metadata owner and re-read current evidence
following lock acquisition. An already inserted, uncommitted input blocks the
permit; after it commits, pending/claimed chat or compact input makes the source
stale. A permit committed first places later input in the next native work.
The source summary never includes that later input.

Latest-root `checkpoint_writes` rows are the saver's `CheckpointTuple.pending_writes`,
not already consumed ancestor delta writes. An unmaterialized task channel,
including an unknown channel, excludes quiescent observation/claim/application;
its payload is retained rather than guessed harmless, deleted or folded into
the summary. Direct or older checkpoint writers outside the supported native
owner remain a compatibility boundary, not a new serialization guarantee.

## Original generation and durable application

One transaction binds a new, fixed compact execution work UUID, generation
attempt UUID and actual registered provider. The ended source work is not the
new executor. Only the transaction's first claimant may call the provider.
A lost claim response is unknown even if the caller had not yet invoked it;
recovery does not create another attempt.

Provider construction must explicitly support `build_single_attempt` in the
provider owner. OpenAI/Anthropic construction sets `max_retries=0`; unsupported
bindings or model overrides fail closed. The original model is fixed; no model
fallback, legacy COMPACT_MAX_ATTEMPTS loop or stale-cache second request runs.
This does not promise exactly-once execution by an external provider.

A completed result retains original attempt, summary, message UUID/time and the
canonical ClosingRequest usage. The permit stamps that exact source checkpoint
as a boundary with its original token anchor, never a later replacement head. Response loss while saving or applying reuses that
same durable result. Unknown generation never automatically calls the provider
again. An unusably short result is REJECTED and closes its native execution.

Application first commits an `applying` permit over the unchanged source. It
then uses the existing compact transition and INIT_CONTEXT owner to materialize
only standing context and the original summary. No DB connection is held over
provider calls or saver I/O. A partial reset resumes that same transition.
After flush, a fresh persisted reader must prove exact marker/result/segment,
original execution, halted idle state, empty reset tail, intact standing head,
original summary at the end and disappearance of the previous source messages.
Only this cold proof and latest-head CAS permit business APPLIED. DONE,
provider success and graph return are insufficient. APPLIED and with-execution
REJECTED retain their pending pointer until the original work is SETTLED with
`ended_at` and its native cancel is actually terminal. `continuation_released`
reports this separate closure; a lost continuation cannot leave an ACTIVE work
behind a released receipt.

A native cancel committed before the application permit retains uncertainty.
A cancel committed after that permit is ordered after the authorized compact:
the serialized continuation finishes its original reset and cold ACK, then the
existing native cancellation owner writes and proves the exact original work's
halt. Terminal receipt replay never projects an old marker over later work.

Before the generic cancellation startup gate, an exact typed compact receiver
with stored original result may resume PREPARED/APPLYING or finish terminal
closure. PREPARED still checks a prior cancel before permitting application;
APPLYING preserves its already committed ordering. A certified successor uses
the public receiver/resource proof and records RECOVERED_STOPPED for the old
cancel, never a fabricated original application marker. No-result uncertainty
cannot use this continuation seam.

## Recovery and authority

Unknown results and native closure remain separate from business application:
[[recovery.ava.okf.md|recovery, retained diagnostics and runner authority]].
