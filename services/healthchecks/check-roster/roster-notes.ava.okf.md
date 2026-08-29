---
type: doc
title: Roster notes — pinning, the remote data plane, the 2026-08-21 audit
description: How the check-roster table is pinned to the module directory and ServiceSpec registrations, the remote-managed data-plane exception to the watchdog repairs, and the 2026-08-21 audit history of the healthcheck roster.
tags:
- services
- healthchecks
- watchdog
---

# Roster notes — pinning, the remote data plane, the 2026-08-21 audit

The roster is pinned to reality by `scripts/lint_doc_roster.py` (set equality against the module directory and the ServiceSpec + hand-added registrations) — a module added, removed, or renamed without updating this table fails the lint.

On a remote-managed data plane (Task #1752) the watchdog drops the local `redis_acl` / `pgbouncer` repairs — see `docs/history/2026-08-28/connection-layer-swappable.md`.

Audit 2026-08-21 (issue #192): all 22 checks present at the audit traversed what they certify. `milvus.py` was the one port-open-only probe and was upgraded to a real RPC; the phantom `task_maintenance` row and seven missing rows are fixed here. The later `brew_pin.py` assertion traverses Homebrew's own read-only pin roster; later additions follow the same traversal rule.
