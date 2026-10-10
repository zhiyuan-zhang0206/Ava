---
type: doc
title: SDK-call sampling
description: Validated live policy snapshots admit SDK calls before their side effects and retain explicit transient-fetch recovery.
tags: [base]
---

# SDK-call sampling

`call_policy.py` defines the validated `SamplingPolicy` and `SamplingPolicyOwner`.
The SDK `Installation` retains that owner. Installed synchronous, asynchronous and
dynamic MCP recorders carry it explicitly into low-level metering and emission;
those entry points require their caller's owner, including a direct `emit()`.
No module cache, current-context registry or event pipeline supplies it.
`telemetry.py` admits each public SDK entry with one policy snapshot before its body
executes. The final `sdk_call` emission uses that same snapshot: a refresh during
the call cannot replace its policy or mask the SDK's return, exception or cancellation.
Nested public SDK fan-out gets its own policy snapshot, event and tally entry.
Plugin wrap layers share one final installed recorder for the same public function.
A direct `emit()` validates policy and propagates errors from its emitter call.

## Refresh and failures

Sampling is opt-in. The default records every event; the execution owner still tallies
every executed public entry even when its event is sampled out. An invalid policy
rejects a call before execution and therefore adds no tally.

The owner's cache refreshes at most once per five seconds without performing
network I/O on the SDK call path. Enrolled runners fetch with a two-second timeout
and one attempt; local processes read their local configuration.
The first read starts a retained refresh attempt. Admission and its real thread
handle prevent another attempt while it remains alive. Each attempt records
completion and its original error; worker-boundary failures are immediately
reported through the no-emitter logger and raised on the owner's next read or stop.

HTTP timeout, network and remote-protocol failures, and HTTP 429, may retain the
last valid policy. Authentication/status errors other than 429, malformed JSON
or configuration, and reader code errors are not transient recovery. The cache
records them and raises on every subsequent read until a successful refresh
provides a new valid snapshot. Failed reads can still trigger a refresh when due;
a later network outage cannot clear an already-recorded configuration error.

Refresh diagnostics use the existing no-emitter logger path; they do not start
an event pipeline to report policy failures. The fixed lazy import of local capture
admission must succeed before the SDK body: import, configuration and code errors propagate to
the caller instead of permitting an uncaptured operation. An absent participant
is a normal no-op supplied by the execution's explicit owner; no import-failure
fallback supplies it.

## Lifetime and shutdown

Full SDK reload explicitly transfers the same sampling owner to the replacement
installation. Faces/config metadata changes retain it too: a reload cannot clear
an invalid policy or restart its refresh cadence. Attachment admission and its
short-lived clients do not own sampling; detach retains the SDK's process lifetime.
Bare Python calls need no invented `AvaContext` or execution tally to use the
installed recorders' sampling owner.

`stop(timeout=2.0)` fences new reads and joins the retained attempt for a finite
budget. It returns `False` if that exact thread remains alive and retains the
handle and late outcome. A later stop can collect the original failure. `close()`
also reports `sdk_sampling_refresh_unfinished`; it does not certify a live attempt
as joined or extend the wait indefinitely.

The execution child collects sampling inside its code-result boundary, before
forming the result envelope. An already-completed unknown failure enters the
existing failure envelope with the actual `code_reached` flag. If the body failed,
its original exception/cause/traceback remain primary and shutdown failure becomes
a note. A completed body with an unfinished refresh retains its actual result;
the diagnostic and retained attempt express the separate shutdown outcome. Slow
sampling does not turn code execution into a timeout or retry its side effects.

The host stops sampling after turn drain and before closing checkpoint pools,
including failed boot cleanup. Other SDK processes register finite `atexit`
cleanup when their installation creates the owner. An `atexit` failure is visible
but does not guarantee a nonzero exit; hard exit does not guarantee a join or
delivery of an unfinished attempt. Refresh does not start a telemetry pipeline.

The SDK recorder snapshots caller identity at entry and explicitly passes it through
`run_metered` / `run_metered_async` to the final event. The call retains its own copy;
an attachment change, nested call or concurrent call cannot relabel it. Low-level
metering and direct `emit()` callers supply their identity mapping explicitly. A caller
identity error rejects admission before the body, preserving the original exception.
The execution child binds an `SdkCallTally` on its process-local `AvaContext` only
while agent code runs. Recorders snapshot that owner alongside caller identity and
pass it explicitly to low-level metering. Ordinary execution threads share the
same lock-protected counts; concurrent calls carry independent snapshots.
`AvaContext.describe()` omits this runtime owner. Calls without an execution tally
still emit events. The recorder snapshots `AvaContext.sdk_capture` at the same
entry and passes it to low-level metering. Capture admission retains each call's
original receipt/gate until its final event; attachment closure rejects new public
entries and seals only after admitted work drains. Calls retain raw gate references
without a current-participant registry or ContextVar. Ordinary SDK/child contexts
have no attachment capture owner, while external attachment contexts carry theirs.
The unused implicit `annotate()` channel has been removed; direct `emit()` can
still receive explicit semantic details.

See the accepted public-entry counting decision in
`docs/decisions/engineering/design/simplification/2026-10-09-explicit-runtime-ownership-boundaries.md`.

## Emission failure ownership

The SDK emitter call owns no network retry or generic sink fallback. An error after
an admitted body succeeds propagates to the caller without executing that body again.
When the body already raised, its exact exception and original cause remain primary;
an emission or receipt-failure recording error is attached as an exception note.
Both outcomes still count the body that executed in the unsampled execution tally.

The original call admission reports capture failure against its own gate, even if
an attachment has closed or another receipt has become bound. Failure is recorded
before admission releases and attempts to seal the drained receipt. A lost event
cannot become an empty complete receipt; a persistence outage retains the existing
pending-failure fence. Manifest capture and admission-release errors propagate
with the same primary/secondary rule. Observation sink delivery retains its existing
best-effort owner; this SDK boundary does not retry either operation.
