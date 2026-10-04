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
[[services/ava_root/docs/ava_root.ava.okf.md|root supervisor]]. It selects readiness
probes for the exact units in the loaded manifest. A missing probe refuses
wiring before any service spawns. Adding a plugin service follows the same
`ServiceSpec.identity_probe` contract as a core service.

The manifest generator (`manifests.py`) also derives each unit's stop window: a
`ServiceSpec` that declares `stop_ceiling_s` (its own SIGTERM cleanup can outlast
root's default window) gets `stop_timeout_s = ceiling + STOP_MARGIN_S`. The
gateway's ceiling is uvicorn's drain budget
(`gateway.gateway_graceful_shutdown_timeout_seconds`) plus the lifespan cleanup
allowance, read from the same setting the launch hands uvicorn; browser-mcp's is
its bounded shutdown steps (`services/browser/shutdown_budget.py`). See
[[services/ava_root/docs/closure.ava.okf.md]].

`HealthMonitor` owns application-service retry scheduling. Only a fresh DOWN
observation may request replacement. The supervisor must settle native custody
before replacement; unknown ownership, unavailable inspection, foreign listeners,
and an operator-held unit cannot authorize recovery. A protocol response alone
cannot certify a service: its responding listener must belong to the captured
root generation. No service probe starts a session or an OS job.

Each round `HealthMonitor` emits `root_unit_failure_state` for every unit in a
failure state ([[services/ava_root/docs/ava_root.ava.okf.md]]); the drill
assembly shares that path.

`RootHealthRounds` drives one service health round and the independent
[[services/ava_root_glue/docs/diagnostics.ava.okf.md|diagnostic roster]]. Diagnostic
failures never acquire service or native-resource lifecycle authority.
`TreeSelfCheck` separately checks process-tree integrity. Their snapshots attach
to root status; deployment drills can assemble the generic monitors without the
host diagnostic roster.

On macOS the permissions helper is the root's ancestor and carries the permission
boundary. Root observes it but cannot restart or upgrade it from inside its own
tree. On Linux the Ava tree starts at root; no helper diagnostic is registered.
Postgres, Redis, and PgBouncer have separate native data-plane custody so they can
remain available during application maintenance.

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
`base.deploy.progress_timeout.CRITICAL_SERVICE_SESSIONS` (Gate, gateway, frontend,
agent-host, and im-bridge) uses 180 seconds; other services use 45 seconds.
Generic root monitoring has no CLI import. A failed replacement still becomes
eligible for bounded retry after its window; accumulated outage/backoff history
is retained during grace, and a fresh healthy response clears it.
