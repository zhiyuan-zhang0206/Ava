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

Eight pseudo-checks have no ServiceSpec. `brew-pin` and `prod-venv` run for both capabilities;
enabled agent-runners add `permissions-helper`, and browser-enabled
agent-runners add `browser-reach`; gateway watchdogs prepend
`redis-acl` and `pgbouncer`, then append marker-gated `lgtm` and the remote
`station-probe`. A station-capable runner also appends `lgtm`. `pg-backup` is a
regular service healthcheck. `--disable-service X` also removes pseudo-checks.

## Gateway order

`redis-acl` → `pgbouncer` → `brew-pin` → `prod-venv` → `gateway` → `im-bridge` → `labeler` →
`heartbeat` → `delivery-watchdog` →
`events-maintenance` → `milvus` or `memory-search` → `frontend` → `pg-backup` →
`pitr-uploader` → `pitr-base-candidate` → `otel-collector` → plugin services →
`lgtm` → `station-probe`. Plugin registration determines the plugin segment.

The Redis ACL repair runs first because services depend on Redis authentication.
The middle follows `build_services()` registration order, including Milvus
before the memory indexer that connects to it on cold start. `pg-backup` owns
its own schedule and the watchdog only probes its last-success health
([[services/gateway_side/backup/backup.ava.okf.md|daily backup]]).

## Agent-runner order

`brew-pin` → `prod-venv` → enabled `permissions-helper` → browser-enabled `browser-reach` →
the derived agent-runner services in
`build_services()` order (`agent-host`, `page-server`, `ops`, the gated browser/computer services, `mcp-daemon`,
then `otel-collector` and plugin services). A station-capable runner appends
`lgtm`.

## Production virtualenv

`prod-venv` is read-only and DB-free. It resolves the installed source through
`shared.cluster_drift.prod_source_dir`, then selects that checkout's interpreter
through `shared.editable_install`. Both children omit inherited `VIRTUAL_ENV`
and `PYTHONPATH`; the import child uses `-I -B` to exclude cwd/user packages and
prevent bytecode writes. Each leg has a 5 s execution deadline with bounded
process-tree cleanup on timeout. Both run every round; missing uv skips only
dependency validation. Errors are deduplicated per watchdog process until the
violation set changes or a healthy round clears it. The smoke imports `ava`,
`pydantic`, `psycopg`, and `fastapi` and rejects hollow namespace packages. This
certifies those imports and dependency metadata, not every installed package
file. Windows skips the check.

Parent: [[services/watchdog/watchdog.ava.okf.md|watchdog]].
