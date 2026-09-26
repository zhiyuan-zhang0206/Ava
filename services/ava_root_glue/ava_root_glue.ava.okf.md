---
type: doc
title: Root deployment wiring
description: Bind the selected service tree to readiness probes, read-only diagnostics, and completed-round observability.
tags:
- services
- ops
---

# Root deployment wiring

`services.ava_root_glue.glue:build_wiring` is the deployment hook for the
[[services/ava_root/ava_root.ava.okf.md|root supervisor]]. It selects readiness
probes for the exact units in the loaded manifest. A missing probe refuses
wiring before any service spawns. Adding a plugin service follows the same
`ServiceSpec.identity_probe` contract as a core service.

`HealthMonitor` owns application-service retry scheduling. Only a fresh DOWN
observation may request replacement. The supervisor must settle native custody
before replacement; unknown ownership, unavailable inspection, foreign listeners,
and an operator-held unit cannot authorize recovery. A protocol response alone
cannot certify a service: its responding listener must belong to the captured
root generation. No service probe starts a session or an OS job.

`RootHealthRounds` drives one service health round and the independent
[[services/ava_root_glue/diagnostics.ava.okf.md|diagnostic roster]]. Diagnostic
failures never acquire service or native-resource lifecycle authority.
`TreeSelfCheck` separately checks process-tree integrity. Their snapshots attach
to root status; deployment drills can assemble the generic monitors without the
host diagnostic roster.

On macOS the permissions helper is the root's ancestor and carries the permission
boundary. Root observes it but cannot restart or upgrade it from inside its own
tree. On Linux the Ava tree starts at root; no helper diagnostic is registered.
Postgres, Redis, and PgBouncer have separate native data-plane custody so they can
remain available during application maintenance.

On Windows the glue registers the explicit `terminal.start` resource operation.
It holds the host allocation-freeze lock from durable birth intent through the
per-terminal owner's ready receipt. Shutdown closes admission and waits for any
earlier birth. The owner creates its target atomically in a non-breakaway Job;
its native pipe and record remain available after root stops. Full terminal stop
observes an empty original Job before publishing closure. An unavailable owner
without that receipt remains unresolved custody, including after owner death.
The terminal backend has its own record namespace; it never adopts named agent
sessions or launches a requester-side fallback. Native Windows CI, not Mac unit
tests, must prove both survival and complete Job closure before startup is enabled.

Durable update and hold completion belongs to the external transition executor,
outside the subtree it stops. Root diagnostics does not invoke the pin, schema,
code-update, or pause-recovery controllers. The old controller graph and hold
watchdog eligibility/attempt state have been removed. No OS watchdog-probe or
hold-watchdog job remains.

The deployment hook initializes the event pipeline as process `ava-root`.
`root_health_expected` announces the observer before its first round, and a zero
expectation explicitly retires it on intentional stop. `root_health_tick` advances
only after service and diagnostic work return, including explicit unavailable
verdicts after observation deadlines. It certifies progress, not service health.
Grafana separately detects expected roots that have no completed sample or a
stale sample; diagnostic verdict events and status carry failure evidence.

Root observation events carry a fixed-length SHA-256 `home_id` of the resolved
home path. Freshness grouping retains it so another cluster on the same host
cannot mask missing rounds; the event body also carries the full home path.

## Per-generation startup observations

Root observes each selected service immediately. Recovery and failure counting
wait until that captured native generation is first observed ALIVE or its startup
budget expires. The deadline is measured from root's retained monotonic launch
time; a changed generation starts a new window, and a response spanning a
replacement is UNAVAILABLE. This is observation of actual readiness, not a claim
that starting means healthy.

Deployment wiring supplies the same readiness tiers used by start:
`shared.deploy_timing.CRITICAL_SERVICE_SESSIONS` (Gate, gateway, frontend,
agent-host, and im-bridge) uses 180 seconds; other services use 45 seconds.
Generic root monitoring has no CLI import. A failed replacement still becomes
eligible for bounded retry after its window; accumulated outage/backoff history
is retained during grace, and a fresh healthy response clears it.
