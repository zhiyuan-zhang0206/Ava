---
type: doc
title: Health Check Roster
description: The healthcheck roster — every checked module with its service, liveness probe, and restart method (respawn / build+start / idempotent bring-ups / ACL re-affirm).
tags:
- services
- healthchecks
- watchdog
---

# Health Check Roster

## Health Check List
| Module | Service | Liveness Probe | Restart Method |
|--------|---------|----------------|----------------|
| `browser.py` | Chrome | CDP `GET /json/version` **verified against this cluster's Chrome** (profile argv token + LISTEN socket on the CDP port, both directions) **and** ava-browser session liveness | `respawn_service` (an orphan of ours is swept + rebuilt; skipped when another unit's browser holds the port — see Notes) |
| `browser_mcp.py` | MCP upstream | Unix socket ping | `respawn_service` |
| `frontend.py` | Next.js | HTTP `curl localhost:3000` | build + start |
| `gateway.py` | Gateway | HTTP `/api/health` 200 + this unit's `home` (`shared.daemon_health.probe_home`) | `respawn_service` |
| `events_maintenance.py` | Events-maintenance | HTTP `/healthz` | `respawn_service` |
| `heartbeat.py` | Heartbeat | HTTP `/healthz` | `respawn_service` |
| `labeler.py` | Labeler | HTTP `/healthz` | `respawn_service` |
| `lgtm.py` | LGTM backend (docker compose, `deploy/lgtm/`) | four readiness endpoints (Loki/Prometheus/Tempo/Grafana) on the compose's fixed host ports; any HTTP answer = alive, connection failure = down; no-op without the `$AVA_HOME/lgtm-host` marker | re-run the idempotent `deploy/lgtm/start.sh` |
| `memory_indexer.py` | Memory-indexer | HTTP `/healthz` | `respawn_service` |
| `milvus.py` | Milvus | TCP connect (gRPC port) | `respawn_service` |
| `ops.py` | Agent-ops | HTTP `/healthz` | `respawn_service` |
| `pgbouncer.py` | per-cluster PgBouncer pooler | admin-console connect on the registry-derived listen port (client scram, no backend hop) — deliberately NOT the end-to-end `SELECT 1`, so a Postgres outage is not answered by restarting a healthy pooler | `ensure_pgbouncer` (the same idempotent bring-up `ava start` runs), verified, then a `pgbouncer_repaired` telemetry event |
| `redis_acl.py` | per-cluster redis ACL | PING as this cluster's username (read from `redis_identity()` using the cluster's `redis_url` — names-as-data, no longer derived from cluster name) | re-affirm ACL user |
| `restarter.py` | Restarter | HTTP `/healthz` | `respawn_service`, **then** a stand-in `RespawnController` pass if the respawn never verifies — see Notes |
| `task_maintenance.py` | Task-maintenance | HTTP `/healthz` | `respawn_service` |
Parent: [[services/healthchecks/healthchecks.ava.okf.md|healthchecks]].
