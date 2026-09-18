---
type: doc
title: Health Check Roster
description: Healthcheck modules, probes, restart methods, and certified traversals.
tags:
- services
- healthchecks
- watchdog
---

# Health Check Roster

<!-- lint:healthcheck-roster-table -->

| Module | Service | Liveness Probe | Restart Method | What it certifies (probe traversal) |
|--------|---------|----------------|----------------|------------------------------------------------|
| `browser.py` | Chrome | CDP `GET /json/version`, profile argv token, listener socket, session liveness, macOS readiness wait marker | `respawn_service` (our orphan is rebuilt; a foreign holder is skipped; a live readiness wait is preserved) | the supervised Chrome serves CDP, or an explicit degraded wait names the delay |
| `browser_mcp.py` | MCP upstream | Unix socket `ping` (JSON request, `ok` reply — the daemon's accept/read loop must answer) | `respawn_service` | the browser-MCP daemon's loop answers; lock-free by design (a slow browser op must not read as death) |
| `browser_reach.py` | shared Chrome → gateway reach | canary fetch through the browser (background `about:blank` target, `no-cors`, wall-clock deadline, closed in `finally`) vs a same-process urllib read; throttled | none — report-only: one ERROR after `browser_reach_failure_threshold` consecutive fails (both readings + recipe), quiet until healthy | the browser's own network face reaches the gateway — breaks when page-level requests hang while the host path stays green (#3921) |
| `brew_pin.py` | Homebrew dependency pin policy | read-only `brew list --pinned` + `brew list --formula`; non-macOS and hosts without brew are silent no-ops | none — one ERROR per drift episode tells the operator to run `brew pin <formula>` manually | every installed formula in the operator-approved manifest remains pinned; it never changes package state |
| `computer_mcp.py` | Computer-use service | Unix socket protocol `ping` (lock-free; does not take the action lock) | `respawn_service` | the computer-MCP daemon's loop answers |
| `mcp_daemon.py` | Shared MCP daemon | Unix socket protocol `ping` | `respawn_and_verify` (probe-confirmed) | the shared MCP daemon's loop answers |
| `delivery_watchdog.py` | Delivery watchdog | HTTP `/healthz` (identity-verified), Liveness beat | `respawn_and_verify` | work loop still ticking (a wedged loop 503s via Liveness) |
| `im_bridge.py` | IM Bridge | HTTP `/healthz` (identity-verified); before respawn, a bounded re-read accepts this unit's matching `name` + `home` even on a stale 503 or pidfile mismatch | `respawn_and_verify` | our daemon still owns and answers the health port; Liveness staleness is warning-only (an IM long poll can block the work loop) |
| `heartbeat.py` | Heartbeat | HTTP `/healthz` (identity-verified), Liveness beat | `respawn_and_verify` | work loop still ticking |
| `labeler.py` | Labeler | HTTP `/healthz` (identity-verified), Liveness beat | `respawn_and_verify` | work loop still ticking |
| `memory_indexer.py` | Memory-indexer | HTTP `/healthz` (identity-verified), Liveness beat | `respawn_and_verify` | work loop still ticking |
| `memory_search.py` | Memory search | real POST `/search` (zero vector, k=1) — the store, not a bare TCP connect | `respawn_and_verify` (probe-confirmed) | the exact-search store answers real searches — breaks exactly when the gateway/indexer calls would |
| `events_maintenance.py` | Events-maintenance | HTTP `/healthz` (identity-verified), per-loop progress deadlines | `respawn_and_verify` | each loop completes bounded work; a timed-out worker wedges its tracker (503) |
| `pg_backup.py` | PG-backup scheduler | HTTP `/healthz` (identity-verified), backup last-success age | `respawn_and_verify` | scheduler progress: fresh dump, boot grace, or running dump; else 503 |
| `pitr_uploader.py` | PITR uploader | HTTP `/healthz` (identity-verified): liveness + disk footprint (gating) + unacked-age (non-gating) | `respawn_and_verify` | loop ticking, disk under hard bound; unacked-age reports degraded without flipping 503 (no restart flaps) |
| `pitr_base_backup.py` | PITR base | HTTP `/healthz` | `respawn_and_verify` | scheduler liveness and durable progress |
| `agent_host.py` | Agent host | HTTP `/healthz` (identity-verified), Liveness beat | `respawn_and_verify` | work loop still ticking |
| `page_server.py` | Page-server supervisor | HTTP `/healthz` (identity-verified), Liveness beat | `respawn_and_verify` | work loop still ticking |
| `ops.py` | Agent-ops | HTTP `/healthz` (identity-verified), update-lock and active-op age | `respawn_and_verify` | responsive ops; work past 30m 503s (saturation is informational) |
| `gateway.py` | Gateway | HTTP `/api/health`: serving + Postgres `SELECT 1`, verified against this unit's `home` (`shared.daemon_health.probe_home`) | `respawn_and_verify` | gateway and DB work; degradation names the component |
| `frontend.py` | Next.js | HTTP 2xx on the **app** port (the entry's always-up gate answers 200 while the app is down), **and the port's LISTEN socket must resolve to the frontend's owner** (session/tree unit, leader or birth-validated descendant); a foreign 2xx reads `PORT_TAKEN`+pid | kill session + `npm run build && npm run start`; refused while a listener outside the session holds the port (no EADDRINUSE loop) | the Next.js app **of this unit** renders — an orphan's 200 cannot mask a failed start |
| `milvus.py` | Milvus | real RPC — `MilvusClient.list_collections` against the cluster's milvus URI (the indexer's own client path); replaced a bare TCP connect — port-open stays green while the server behind it is unusable (issue #192) | `respawn_service` | milvus serves RPCs — breaks exactly when the indexer's calls would |
| `pgbouncer.py` | per-cluster PgBouncer pooler | admin-console connect on loopback (client scram, no backend hop) plus a socket-table check for the reachable-address listener — NOT end-to-end `SELECT 1`, so a Postgres outage is not answered by restarting a healthy pooler; no hairpin through host networking | `ensure_pgbouncer` (the idempotent bring-up `ava start` runs), verified, then a `pgbouncer_repaired` event | the pooler's protocol answers on loopback; the OS socket table proves its public front door is bound |
| `redis_acl.py` | per-cluster redis ACL | PING as this cluster's username (read from `redis_identity()` using the cluster's `redis_url` — names-as-data, no longer derived from cluster name) | re-affirm ACL user | the cluster identity authenticates to redis — every component's redis path (the 0004 guardrail) |
| `otel_collector.py` | OTel collector sidecar | POST `/v1/traces` to the OTLP receiver must return 2xx; listeners on :4318/:8888 must resolve to this unit's collector binary + live session record; non-LGTM gateways warn and skip, pure runners keep relay behavior | one 5 s SIGTERM window for a verified stale holder, then a verified SIGKILL fallback + `respawn_and_verify`; a survivor stays loud | the supervisor-owned OTLP listener the agents export through answers, not an old collector that kept the port |
| `lgtm.py` | local LGTM backends (`deploy/lgtm/`) | three readiness endpoints (Loki/Prometheus/Grafana) on fixed host ports; remote Tempo excluded (its failure must not restart local backends); any HTTP answer = alive, connection failure = down; Linux adds canonical-unit ownership; no-op without the `$AVA_HOME/lgtm-host` marker or station capability | re-run the idempotent `deploy/lgtm/start.sh` | each local backend's readiness listener answers (its own health traversal) |
| `permissions_helper.py` | AvaPermissionsHelper | socket `ping` (3 s); on failure a launchd `job state` classification (LWCR-stuck named); non-macOS no-op | third failure: bootout/bootstrap; failed repair escalates + backoff-retries | helper protocol answers; not LWCR-stuck |

Pinning, the data-plane exception, and the 2026-08-21 audit: [[roster-notes.ava.okf.md]]
