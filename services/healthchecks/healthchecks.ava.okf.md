---
type: doc
title: Healthchecks — Service Health Probes
description: A collection of health-check scripts for each background service — called by watchdog every 60s to determine if a service is alive, triggering restart when dead. Each service has an independent module defining its own "alive" criteria and restart method.
tags:
- infra
- service
- infrastructure-management
---

# Healthchecks — Service Health Probes

## What is it
A collection of health-check scripts for each background service — called by watchdog every 60s to determine if a service is alive, triggering restart when dead. Each service has an independent module defining its own "alive" criteria and restart method. Most daemons have already switched from pidfile probing to their own HTTP `/healthz` (the bound port is a socket-level signal, immune to false positives from a missing pidfile during cold start).

**Role attribution**: both sides — not an independent process; each probe module is called by the watchdog of the corresponding capability (`gateway-watchdog` / `agent-runner-watchdog`) according to its `ServiceSpec.healthcheck_module`.

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

## Core Responsibilities
- **Independent liveness logic**: each service defines its own "alive" criteria (HTTP `/healthz` or `/api/health` 200 / CDP / unix socket ping / TCP connect / redis PING) — there are no longer any pidfile-probed services in the table
- **Identity before liveness, and the operator sees the same verdict**: every probe that CAN prove the answering process is this unit's does, and the same probe is what `ava status` / `ava cluster health-probe` / `ava start`'s readiness gate run, via `ServiceSpec.identity_probe`. The watchdog and the human can no longer be told different things about one port ([[services/healthchecks/terminal-verdict/terminal-verdict.ava.okf.md]])
- **Restart trigger**: on a respawnable failure calls `shared.service_respawn.respawn_service` to rebuild the session; a terminal verdict reports instead of respawning
- **Frontend special handling**: Next.js has no PID hook; probed via HTTP curl, restart = kill session + `npm run build && npm run start` (30-60s)
- **The hand-added checks (`pgbouncer` / `redis-acl` / `lgtm`) have no ServiceSpec**: pgbouncer and redis are native per-cluster processes, the LGTM stack is a docker compose project — none are sessions — so the watchdog adds them by hand (redis-acl + pgbouncer prepended, lgtm appended). pgbouncer runs FIRST because when enabled it IS every consumer's `AVA_DB_URL`; all three carry `requires_db=False`, since a DB-scoped block exists exactly when the database is unreachable and holding back these repairs would deadlock (data plane) or blind the operator (observability)
- **redis-acl exception**: not probing+restarting a process; instead on PING failure it uses box-level admin to replay `ensure_cluster_redis_acl` to repair the runtime-lost per-cluster ACL (gateway-only, reoccurs on every redis bounce)

## Key Dependencies
- [[services/watchdog/watchdog.ava.okf.md]] — sole caller (gateway / agent-runner each run the batch for their own capability)
- `shared.service_respawn.respawn_service` — unified restart entry

## Notes
- **A probe always returns a verdict, never an exception** — enforced by a total wrapper per probe, not by widening catch tuples, and failing closed. Also covers browser's two-probe rule, redis-acl's documented exception, and the same rule generalized to the whole `main()` (nothing failable between the dead verdict and the respawn): [[services/healthchecks/probe-contract/probe-contract.ava.okf.md]].
- **Not every dead verdict is respawnable** — a `/healthz` answered by another unit's daemon is terminal: reported at ERROR with a distinct exit code and NOT respawned, because no respawn this unit can perform frees that port. The three-way verdict and where the line falls: [[services/healthchecks/terminal-verdict/terminal-verdict.ava.okf.md]]. The shared `main()` that applies it is `shared.service_respawn.run_keepalive`, used by all eight `/healthz`-probed healthchecks (gateway, ops, restarter, labeler, heartbeat, memory-indexer, events-maintenance, task-maintenance).
- **restarter's stand-in dispatch** — the one healthcheck that does work beyond probe+respawn, and the only one that touches the DB: [[services/healthchecks/restarter-standin.ava.okf.md]].
- browser_mcp's ping deliberately avoids an upstream round trip or daemon serial lock — slow browser operations could hold the lock longer than the probe timeout, causing false kills
- The frontend healthcheck uses HTTP `curl localhost:3000` (`npm run start` doesn't expose a PID); it's **truly** special in its **restart method** — kill session + `npm run build && npm run start` (build gate), not in its probing method (gateway uses `/api/health`, browser uses CDP, milvus uses TCP, browser_mcp uses socket, redis_acl uses PING — none of those are `/healthz`)
- A healthcheck module is invoked ⟺ some `build_services()` `ServiceSpec.healthcheck_module` declares it (the hand-added gateway pseudo-checks above are the exception) — watchdog derives the roster **single-source** from that field ([[services/watchdog/watchdog.ava.okf.md]]). Merely placing a module in `services/healthchecks/` won't cause it to be called. A healthcheck module **does not have to** live in this directory: `healthcheck_module` is just an importable dotted module string; services registered by plugins place their healthchecks in their own namespace (e.g., `ava_builtins.plugins.ava_fleet.task_maintenance.healthcheck` is declared by the `task-maintenance` ServiceSpec, and watchdog still pulls it in via importlib and keeps it alive every 60s).
