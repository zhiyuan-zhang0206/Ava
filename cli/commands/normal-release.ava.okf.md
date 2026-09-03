---
type: doc
title: Pending normal release continuation
description: Exact selector and service effects in the existing per-unit updater.
---

# Pending normal release continuation

`_update_agent_runner --normal-release` is an internal continuation, not a second
release controller. A bootstrap request may alternatively reference this normal
request before its first stop: the same updater prepares both stages, completes
the restricted hop, then continues under the original flock/operation/deadline.
No mutation endpoint is added to bootstrap. Standalone continuation consumes a private request, the existing completed
restricted-bootstrap handoff, the exact exited predecessor, a fully verified
image and complete preparation receipt. Unsupported normal readiness transports
refuse in preparation while bootstrap still serves. Source update flags and
git/uv/converge fallbacks are unavailable in this mode.

The same local updater flock and handoff journal survive the bounded wait for
the existing deployment pending record. A complete adopted collection and an
actual schema migration receipt authorize selector CAS. Only then can the exact
prepared normal service set start. The producer checks the native session and
listening child PID/birth, actual executable/command, and existing service health
response. Python normal services report their actually loaded module and image;
development responses remain unchanged. Native frontend/collector probes also
require native listener ownership, not just an HTTP success.

Every permission read has the original challenge budget. No retry renews it.
Each effect gets fresh pending authorization. PostgreSQL and OS/filesystem
effects are not one atomic transaction; losing authority stops further effects
and retains the journal. A failed start is not force-cleared. Restoration needs
a newly authorized reverse plan, not an expired previous permission.

Per-unit readbacks are recorded in the same pending evidence for the all-unit
coordinator. Current publication, not a local successful launch, is the terminal
condition. This service permission does not authorize ordinary agent admission.

## Incomplete callers and support

This continuation does not yet implement normal/source first handoff, complete
non-session/job quiesce, all-unit collection/migration orchestration, or checked
normal recovery. The preparation receipt's unknown closure is not promoted to a
positive permit. Unix-only/native services without a verified readiness adapter
remain pre-stop refusals. These are implementation gaps, not claims awaiting CI.

CI contracts cover exact selector serialization, unsupported/mutable commands,
CLI source isolation, and retained unfinished handoffs. Actual normal full-roster
cold launch and the complete distributed transition remain required evidence.
