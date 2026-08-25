---
title: Loki WAL disk-full throttle raised to 0.95 (Task #1626)
---

# Loki WAL disk-full throttle raised to 0.95

**Date:** 2026-08-25
**Task:** #1626 (FleetView 卡顿 + 缺边复查)

## Incident

The data volume (`/System/Volumes/Data`, 228GiB) has hovered at ~90% capacity
since 2026-08-24 evening. Loki 3.7.6's default WAL disk-full write throttle
(`ingester.wal.disk_full_threshold`, default 0.9) therefore flapped: whenever
usage crossed 90%, Loki refused every push (`Ingester is shutting down` 503s,
collector retry then drop), silently losing the audit event stream that feeds
the fleet graph edges. Two separate windows: 08-24 23:09→08-25 00:23 and
08-25 11:58→12:44 (+ brief 01:46 crossing). Spawn events for agents 3409/3410
were dropped; the fleet view showed fresh nodes with zero edges.

## Fix

- Raise the throttle to **0.95** in both `deploy/lgtm/native/config/loki.yaml`
  (live) and `deploy/lgtm/config/loki.yaml` (container rollback asset):
  `ingester.wal.disk_full_threshold: 0.95`.
- Pin it in `shared/loki_index_labels.validate_loki_deploy_config` so a
  re-render cannot silently fall back to the flapping default.
- Field name verified against the deployed binary: `loki -config.file=<cfg>
  -verify-config` accepts `ingester.wal.disk_full_threshold` (the top-level
  `ingester.wal_disk_full_threshold` spelling is NOT a valid field in
  Loki 3.7.6's `ingester.Config` — attempted on 08-25 12:41, crash-looped the
  launchd job until reverted).

## Why 0.95, not 0 or 1.0

0.95 keeps a real disk-full guard (~11GiB cushion) while tolerating the
current 89-91% oscillation. It is a **stopgap**: the durable fix is freeing
disk space (the ~/.ava-log retention and user-side cleanup are tracked
separately; 405 coordinates).

## Ops note (what NOT to repeat)

Directly editing the prod template + `ava lgtm on` + kickstart is the fast
path but bypasses review — the wrong field name crash-looped Loki for ~3
minutes (12:41-12:44) and took down every Loki-backed read. This change now
ships through the normal PR + QA + rollout flow instead.
