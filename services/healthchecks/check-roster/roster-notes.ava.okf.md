---
type: doc
title: Roster notes — probe scope, pinning, and the data plane
description: How the check-roster table is pinned to the module directory and ServiceSpec registrations, the remote-managed data-plane exception to the watchdog repairs, and the 2026-08-21 audit history of the healthcheck roster.
tags:
- services
- healthchecks
- watchdog
---

# Roster notes — probe scope, pinning, and the data plane

The roster is pinned to reality by `scripts/lint_doc_roster.py` (set equality against the module directory and the ServiceSpec + hand-added registrations) — a module added, removed, or renamed without updating this table fails the lint.

On a remote-managed data plane (Task #1752) the watchdog drops the local `redis_acl` / `pgbouncer` repairs — see `docs/history/2026-08-28/connection-layer-swappable.md`.

Audit 2026-08-21 (issue #192): all 22 checks present at the audit traversed what they certify. `milvus.py` was the one port-open-only probe and was upgraded to a real RPC; the phantom `task_maintenance` row and seven missing rows are fixed here. The later `brew_pin.py` assertion traverses Homebrew's own read-only pin roster; later additions follow the same traversal rule.

## PgBouncer probe scope

The pooler probe connects to the loopback admin console using client SCRAM,
without a backend hop. It does not run an end-to-end `SELECT 1`: a Postgres
outage must not trigger a healthy pooler's restart.
`pgbouncer_public_listener_reachable` also reads the OS socket table for the
configured reachable-address listener; it does not dial through host networking,
where hairpin filtering can reject a working bind. PgBouncer can remain alive
after one bind fails, so loopback alone cannot certify its public front door.
Repair calls `ensure_pgbouncer`, the same idempotent bring-up as `ava start`,
verifies the result, then emits `pgbouncer_repaired`.
