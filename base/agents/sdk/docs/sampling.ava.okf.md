---
type: doc
title: SDK-call sampling
description: Validated live policy snapshots admit SDK calls before their side effects and retain explicit transient-fetch recovery.
tags: [base]
---

# SDK-call sampling

`call_policy.py` owns the validated `SamplingPolicy` and its process-local cache.
`telemetry.py` admits each outer SDK call with one policy snapshot before its body
executes. The final `sdk_call` emission uses that same snapshot: a refresh during
the call cannot replace its policy or mask the SDK's return, exception or cancellation.
Nested SDK fan-out shares the outer admission and produces no additional event.
A direct `emit()` validates policy before entering event-sink error handling.

## Refresh and failures

Sampling is opt-in. The default records every event; `recording()` still tallies
every executed outer call even when its event is sampled out. An invalid policy
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
side-channel contract. Local capture admission and caller-identity capture have
their own existing error boundaries; this policy does not change them.
