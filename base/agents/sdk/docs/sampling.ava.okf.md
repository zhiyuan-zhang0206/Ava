---
type: doc
title: SDK-call sampling
description: Validated live policy snapshots admit SDK calls before their side effects and retain explicit transient-fetch recovery.
tags: [base]
---

# SDK-call sampling

`call_policy.py` owns the validated `SamplingPolicy` and its process-local cache.
`telemetry.py` admits each public SDK entry with one policy snapshot before its body
executes. The final `sdk_call` emission uses that same snapshot: a refresh during
the call cannot replace its policy or mask the SDK's return, exception or cancellation.
Nested public SDK fan-out gets its own policy snapshot, event and tally entry.
Plugin wrap layers share one final installed recorder for the same public function.
A direct `emit()` validates policy before entering event-sink error handling.

## Refresh and failures

Sampling is opt-in. The default records every event; the execution owner still tallies
every executed public entry even when its event is sampled out. An invalid policy
rejects a call before execution and therefore adds no tally.

The existing cache refreshes at most once per five seconds without performing
network I/O on the SDK call path. Enrolled runners fetch with a two-second timeout
and one attempt; local processes read their local configuration.

HTTP timeout, network and remote-protocol failures, and HTTP 429, may retain the
last valid policy. Authentication/status errors other than 429, malformed JSON
or configuration, and reader code errors are not transient recovery. The cache
records them and raises on every subsequent read until a successful refresh
provides a new valid snapshot. Failed reads can still trigger a refresh when due;
a later network outage cannot clear an already-recorded configuration error.

Refresh diagnostics use the existing no-emitter logger path; they do not start
an event pipeline to report policy failures. Event-sink failures remain a separate
side-channel contract. The fixed lazy import of local capture admission must
succeed before the SDK body: import, configuration and code errors propagate to
the caller instead of permitting an uncaptured operation. An absent participant
is a normal no-op decided by the manifest gate itself; its receipt/admission
lifecycle remains unchanged.

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
still emit events. Capture admission retains each call's original receipt/gate
until its final event; attachment closure seals only after admitted work drains.
The unused implicit `annotate()` channel has been removed; direct `emit()` can
still receive explicit semantic details.

See the accepted public-entry counting decision in
`docs/decisions/engineering/design/simplification/2026-10-09-explicit-runtime-ownership-boundaries.md`.
