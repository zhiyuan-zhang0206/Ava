---
type: doc
title: Watchdog Healthcheck Checklist
description: How each capability's watchdog derives and orders its service healthchecks and hand-added pseudo-checks.
tags:
- services
- watchdog
- healthchecks
---

# Watchdog Healthcheck Checklist

## Derived services and hand-added checks

The service checklist is derived from `build_services()`'s
`ServiceSpec.healthcheck_module` ([[services.ava.okf.md|single truth]]). A new
service registered there is monitored without another watchdog registration,
including plugin services. Role membership and config/capability gating reuse
the `services_for_capabilities_annotated` view used by `ava start`, so a service
that start gates out is not resurrected; the reason is logged at debug and
surfaced by `ava status`.

Five pseudo-checks have no ServiceSpec. `brew-pin` runs for both capabilities;
gateway watchdogs prepend `redis-acl` and `pgbouncer`, then append marker-gated
`lgtm` and scheduled `pg-backup`. `--disable-service X` also removes
pseudo-checks.

## Gateway order

`redis-acl` → `pgbouncer` → `brew-pin` → `gateway` → `labeler` → `heartbeat` →
`events-maintenance` → `milvus` → `frontend` → `task-maintenance` →
`memory-indexer` → `lgtm` → `pg-backup`.

The Redis ACL repair runs first because services depend on Redis authentication.
The middle follows `build_services()` registration order, including Milvus
before the memory indexer that connects to it on cold start. `pg-backup` is last:
it is a scheduled job piggybacking on the tick, not a healthcheck
([[backup.ava.okf.md|daily backup]]).

## Agent-runner order

`brew-pin` → `restarter` → `ops`, conditionally followed by `browser` and
`browser-mcp` when browser service configuration and host capability allow them.
The derived segment follows `build_services()` registration order.

Parent: [[services/watchdog/watchdog.ava.okf.md|watchdog]].
