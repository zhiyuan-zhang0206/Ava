---
type: doc
title: Restricted immutable ops updater hop
description: Existing updater ownership, same-endpoint bootstrap replacement and retained-A compensation.
status: current
---

# Restricted immutable ops updater hop

The existing per-unit updater's internal `--bootstrap-hop` branch accepts a
private request referencing two verified image contexts and the full prepared
inventory receipt. It never resolves a source checkout, syncs packages, migrates,
converges, changes a selector or starts ordinary services. Return code 3 denotes
restricted bootstrap success, deliberately not normal updater success.

Before changing a service, it verifies
the candidate's loaded code, both sealed images, canonical registered unit and
live deployment operation, coherent explicit child projection, full inventory
and the predecessor's PID/birth against the existing updater handoff record.
Only an already-running restricted A native exec session and exact inventoried
Linux cron commands are admitted. Ordinary/source A, another live updater,
additional sessions, unknown jobs and unsupported platforms refuse before stop.

The updater atomically replaces that exact dead predecessor marker with its own
running generation. A separate bounded, versioned recovery envelope journals
compensating inputs before quiesce. An
exact native cron replacement requires an independent readback; the official
named session lifecycle stops A and starts B at the same endpoint. Authenticated
bootstrap readback names its actual PID/birth and resolved loaded module, image
and home. The updater cross-checks these against the local exact native exec
session and command; an unexpected child or substituted identity refuses.
Neither port reachability nor supervisor disappearance alone proves writer exit.

Admission completely verifies both images before quiesce. Endpoint challenges
reuse those invocation-local verified paths and recheck the manifest binding;
they do not hash the entire retained environment inside the readiness deadline.
No verification result is cached across updater invocations. A live or unknown
predecessor is rejected before image traversal and checked again afterward.
Each startup wait uses at most half the outstanding challenge, reserving the
remaining authority window for compensation. Transient unproven exec identity
is only retried as not-ready; it never authorizes a signal or successful closure.
Pre-quiesce rejects a remaining challenge shorter than this invocation's measured
validation cost. This is a lower-bound refusal, not a claimed cold-boot upper
bound or proof that recovery will fit. The recovery envelope records at most 64
phase times;
they are observation only, never authority, and a new updater PID resets the
comparable elapsed interval instead of inventing a cross-process duration.

Every resume re-runs the native inventory producer and requires all prepared
service-roster, unit, and receipt facts to remain exact. Only the sole `ava-ops`
process may differ, and only after its record, command, process identity, and
verified A/B image are checked. Its launcher set may remain exact or be empty
after accounted quiesce; the raw table must still equal the journaled original
or its exact restricted-entry removal before any restore. Candidate startup failure compensates to the
verified restricted A and restores
only the unchanged original cron definition. Dead updater recovery reclaims the
same generation and observes actual A/B state. A malformed envelope is retained
for diagnosis rather than silently interpreted or cleared. A post-fork outcome without a new
session record is ambiguous and preserves the journal. Expired authority does
not authorize recovery writes. External concurrent crontab editors are outside
the managed-writer mutex; observed drift refuses rather than overwriting it.

## Proof and remaining first-cutover gates

CI builds two different real generations of the same application revision,
hides the checkout, and drives the actual updater entry against native PG,
native sessions and isolated native cron. It exercises live-predecessor refusal,
restricted A-to-B, real B bind failure restoring A, and a crash after A exit
followed by dead-owner recovery. Focused regressions cover predecessor CAS,
resume inventory drift, malformed-envelope retention, bounded phase evidence,
and fork-before-record ambiguity. This is not mixed-version compatibility proof.

The first production source-to-image transition still requires a genuinely
bootable normal-service LKG that survives operation expiry, actual old imported
orchestrator handoff, all registered unit/writer/job closure, normal-service
readback and expected-predecessor selector publication. Windows full image
closure and macOS native quiesce are unsupported here. The normal start gate and
protocol-v1 admission remain closed. A retained source path/argv or bootstrap
health response does not satisfy any of those remaining gates.
