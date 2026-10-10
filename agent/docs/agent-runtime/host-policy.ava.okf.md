---
type: doc
title: Hosted Admission and Cache Policy
description: Per-host admission limits, live model readers and warm-cache bounds.
tags: []
---

# Hosted Admission and Cache Policy

Active agents have no default admission limit: `AVA_HOST_MAX_CONCURRENT_TURNS=0`
allows another agent to start while existing agents wait on models or tools.
A positive value opts into the host limit; `services/agent_runner/agent_host/scheduling/admission.py`
(`TurnAdmission`) then serves excess continuations as a fair queue: one ticket
per agent (`TurnScheduler`'s single flight), arrival-order FIFO, a completed
turn's next request taken at the tail — ticket rotation, no starvation. Queue
depth, waiter ages and served-wait counters are exposed on the daemon's
`/stats`; a wait past `AVA_HOST_ADMISSION_WAIT_ALERT_SECONDS` reports one
`host_admission_wait_exceeded` anomaly event per episode. A queued turn is
explicitly exempt from the dispatcher's stall cancellation — its progress clock
is silent by design and cancelling it would only re-queue it at the tail.
Per-agent single-flight and resource settlement still apply. Active runtimes
remain resident; completed turns immediately trim the warm cache to its
size/idle budget. Host memory, exec capacity and provider quotas remain separate
constraints.

The daemon supplies `HostPolicy` (`agent_host/runtime.py`) explicitly: admission
capacity is read at construction, cache limits at each eviction, and the model
and override readers at the existing admission, slice and runtime-build boundaries.
Agent pins retain precedence. Policy inputs are per host; a live default update
still follows the existing runtime-cache invalidation rules.

New runners advertise zero-limit support in their bootstrap request. The gateway
projects zero to the legacy positive default for older runners, so partial
updates cannot turn their admission semaphore into a permanent zero-slot wait.
