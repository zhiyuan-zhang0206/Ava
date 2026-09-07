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

| Module | Service | Liveness Probe | Restart Method | What it certifies (how the probe traverses it) |
|--------|---------|----------------|----------------|------------------------------------------------|
| `browser.py` | Chrome | CDP `GET /json/version`, profile argv token, listener socket, session liveness, and macOS readiness wait marker | `respawn_service` (our orphan is rebuilt; another unit's holder is skipped; a live macOS readiness wait is preserved) | the supervised Chrome of this cluster serves CDP, or an explicit degraded wait names why launch is safely delayed |
| `browser_mcp.py` | MCP upstream | Unix socket protocol `ping` (JSON request, `ok` reply — the daemon's accept/read loop must answer) | `respawn_service` | the browser-MCP daemon's loop answers requests; lock-free by design (a slow browser op must not read as death) |
| `brew_pin.py` | Homebrew dependency pin policy | read-only `brew list --pinned` + `brew list --formula`; non-macOS and hosts without brew are silent no-ops | none — one ERROR per drift episode tells the operator to run `brew pin <formula>` manually | every installed formula in the operator-approved manifest remains pinned; it never changes package state |
| `computer_mcp.py` | Computer-use service | Unix socket protocol `ping` (lock-free; does not take the action lock) | `respawn_service` | the computer-MCP daemon's accept/read loop answers |
| `mcp_daemon.py` | Shared MCP daemon | Unix socket protocol `ping` | `respawn_and_verify` (probe-confirmed) | the shared MCP daemon's loop answers |
| `delivery_watchdog.py` | Delivery watchdog | HTTP `/healthz` (identity-verified) with Liveness beat | `respawn_and_verify` | our daemon's work loop is still ticking (a wedged loop 503s via Liveness) |
| `im_bridge.py` | IM Bridge | HTTP `/healthz` (identity-verified); before respawn, a second bounded read accepts this unit's matching `name` + `home` even on a stale 503 or pidfile mismatch | `respawn_and_verify` | our daemon still owns and answers the health port; Liveness staleness is warning-only because an IM long poll can legitimately block the work loop |
| `heartbeat.py` | Heartbeat | HTTP `/healthz` (identity-verified) with Liveness beat | `respawn_and_verify` | our daemon's work loop is still ticking |
| `labeler.py` | Labeler | HTTP `/healthz` (identity-verified) with Liveness beat | `respawn_and_verify` | our daemon's work loop is still ticking |
| `memory_indexer.py` | Memory-indexer | HTTP `/healthz` (identity-verified) with Liveness beat | `respawn_and_verify` | our daemon's work loop is still ticking |
| `memory_search.py` | Memory search | real POST `/search` (zero vector, k=1) — traverses the store, not a bare TCP connect | `respawn_and_verify` (probe-confirmed) | the exact-search store answers real searches — breaks exactly when the gateway/indexer search calls would break |
| `events_maintenance.py` | Events-maintenance | HTTP `/healthz` (identity-verified), per-loop progress with hard deadlines | `respawn_and_verify` | each loop completes bounded work; a timed-out worker wedges its tracker and 503s |
| `pg_backup.py` | PG-backup scheduler | HTTP `/healthz` (identity-verified), backup last-success age | `respawn_and_verify` | scheduler progress: a fresh dump, boot grace, or running dump; otherwise 503 |
| `pitr_uploader.py` | PITR uploader | HTTP `/healthz` (identity-verified): liveness + disk footprint (gating) + unacked-age (non-gating) | `respawn_and_verify` | loop ticking and disk under hard bound; unacked-age conditions report degraded without flipping 503 (no restart flaps) |
| `pitr_base_backup.py` | PITR base | HTTP `/healthz` | `respawn_and_verify` | scheduler liveness and durable progress |
| `agent_host.py` | Agent host | HTTP `/healthz` (identity-verified) with Liveness beat | `respawn_and_verify` | our daemon's work loop is still ticking |
| `page_server.py` | Page-server supervisor | HTTP `/healthz` (identity-verified) with Liveness beat | `respawn_and_verify` | our daemon's work loop is still ticking |
| `ops.py` | Agent-ops | HTTP `/healthz` (identity-verified), update-lock and active-op age | `respawn_and_verify` | responsive ops; work past 30m 503s (saturation is informational) |
| `gateway.py` | Gateway | HTTP `/api/health`: serving + Postgres `SELECT 1`, verified against this unit's `home` (`shared.daemon_health.probe_home`) | `respawn_and_verify` | gateway and DB work; degradation names the component |
| `frontend.py` | Next.js | HTTP 2xx on the **app** port (not the entry port — the entry's always-up gate answers 200 while the app is down) | kill session + `npm run build && npm run start` | the Next.js app renders (a dead app cannot answer 2xx) |
| `milvus.py` | Milvus | real RPC — `MilvusClient.list_collections` against the cluster's milvus URI (the indexer's own client path); a bare TCP connect was replaced because a port-open probe stays green while the server behind the port is unusable (issue #192) | `respawn_service` | milvus serves RPCs — the check breaks exactly when the indexer's calls would break |
| `pgbouncer.py` | per-cluster PgBouncer pooler | admin-console connect on loopback (client scram, no backend hop) plus a local socket-table check for the reachable-address listener — deliberately NOT the end-to-end `SELECT 1`, so a Postgres outage is not answered by restarting a healthy pooler; the public-bind check does not hairpin through host networking | `ensure_pgbouncer` (the same idempotent bring-up `ava start` runs), verified, then a `pgbouncer_repaired` telemetry event | the pooler's own protocol answers on loopback, and the OS socket table proves its public front door is bound |
| `redis_acl.py` | per-cluster redis ACL | PING as this cluster's username (read from `redis_identity()` using the cluster's `redis_url` — names-as-data, no longer derived from cluster name) | re-affirm ACL user | the cluster identity authenticates to redis — the exact path every component's redis call takes (the 0004 guardrail) |
| `otel_collector.py` | OTel collector sidecar | POST `/v1/traces` to the OTLP receiver must return 2xx, and listeners on :4318/:8888 must resolve to this unit's collector binary + live session record; non-LGTM gateways warn and skip, pure runners retain relay behavior | give each verified stale holder one 5 s SIGTERM window before a verified SIGKILL fallback, then `respawn_and_verify`; a survivor remains loud/down | the supervisor-owned OTLP listener the agents export through answers, not an old collector that kept the port |
| `lgtm.py` | local LGTM backends (`deploy/lgtm/`) | three readiness endpoints (Loki/Prometheus/Grafana) on their native fixed host ports; remote Tempo is excluded so its failure cannot restart local backends; any HTTP answer = alive, connection failure = down; no-op without the `$AVA_HOME/lgtm-host` marker or station capability | re-run the idempotent `deploy/lgtm/start.sh` | each local backend's readiness listener answers (its own health traversal) |
| `permissions_helper.py` | AvaPermissionsHelper | socket `ping` (3 s); non-macOS no-op | third failure: bootout/bootstrap once; verify next round | the signed helper protocol answers |

Notes on how the table is pinned to the code, the remote-managed data-plane exception, and the 2026-08-21 audit: [[services/healthchecks/check-roster/roster-notes.ava.okf.md]]
