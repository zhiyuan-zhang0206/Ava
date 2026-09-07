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

Six pseudo-checks have no ServiceSpec. `brew-pin` runs for both capabilities;
enabled agent-runners add `permissions-helper`; gateway watchdogs prepend
`redis-acl` and `pgbouncer`, then append marker-gated `lgtm` and the remote
`station-probe`. A station-capable runner also appends `lgtm`. `pg-backup` is a
regular service healthcheck. `--disable-service X` also removes pseudo-checks.

## Gateway order

`redis-acl` → `pgbouncer` → `brew-pin` → `gateway` → `im-bridge` → `labeler` →
`heartbeat` → `delivery-watchdog` →
`events-maintenance` → `milvus` or `memory-search` → `frontend` → `pg-backup` →
`pitr-uploader` → `pitr-base-candidate` → `otel-collector` → plugin services →
`lgtm` → `station-probe`. Plugin registration determines the plugin segment.

The Redis ACL repair runs first because services depend on Redis authentication.
The middle follows `build_services()` registration order, including Milvus
before the memory indexer that connects to it on cold start. `pg-backup` owns
its own schedule and the watchdog only probes its last-success health
([[backup.ava.okf.md|daily backup]]).

## Agent-runner order

`brew-pin` → enabled `permissions-helper` → the derived agent-runner services in
`build_services()` order (`agent-host`, `page-server`, `ops`, the gated browser/computer services, `mcp-daemon`,
then `otel-collector` and plugin services). A station-capable runner appends
`lgtm`.

Parent: [[services/watchdog/watchdog.ava.okf.md|watchdog]].
