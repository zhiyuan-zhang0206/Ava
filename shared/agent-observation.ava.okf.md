---
type: doc
title: Agent observation evidence
description: Independent machine-probe and runtime-lease clocks, without changing lifecycle intent.
tags: [shared, observability]
---

# Agent observation evidence

`agent_observation.py` projects existing `machine_probe.last_probe_at` and
`agents_meta.lease_expires_at` into list and inspector responses. A machine
probe only observes machine reachability. Its deadline uses the existing
heartbeat cadence and consecutive-failure window, shared with the writer.
The liveness merge preserves the actual probe timestamp; absent probes remain
unknown rather than gaining a synthetic successful observation.

Lifecycle remains running, idling, or terminated. An unexpired runtime lease
does not prove ownership: this compatibility slice reports runtime owner as
unknown until a generation-bound ownership contract is available. Execution
progress and `last_active_at` are not heartbeats. UI renders observation age
and absolute deadlines using its existing clock, without extra network probes.
The existing per-row minute timers remain; no additional list timer is added.

`AgentAvailability` is a separate creation/start observation on `AgentCard`
and `AgentSnapshot`. The heartbeat status probe retains its existing
`agent_host_online` verdict in `machine_probe` without changing what that probe
means. A host verdict is fresh for two 60-second liveness intervals; a durable
per-agent admission result is fresh for five minutes. Both clocks must be fresh
before reporting `admitted` or `admission_refused`. A fresh host-down verdict
takes precedence over older admission history. A fresh host-up verdict with no
admission result means `awaiting_admission`; missing or stale evidence is
`unknown`. `observed_at` is when this projection was evaluated and `evidence_at`
is the source observation time. `admitted` means the ownership row was claimed,
not that a message was claimed or a turn completed. Lifecycle `status` is
unchanged.

Inspector `shells_available=false` means the runner observation failed;
`true` with an empty list means a successful empty result. Missing availability
from older servers is unknown. Known RPC failures emit a bounded-reason metric
and a warning; malformed results and database errors are not converted to empty
successes.
