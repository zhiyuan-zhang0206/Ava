---
type: doc
title: Health Check Roster
description: The healthcheck roster — every checked module with its service, liveness probe, restart method, and what the probe actually certifies (the traversal that keeps the check from staying green through an outage).
tags:
- services
- healthchecks
- watchdog
---

# Health Check Roster

<!-- lint:healthcheck-roster-table -->

| Module | Service | Liveness Probe | Restart Method | What it certifies (how the probe traverses it) |
|--------|---------|----------------|----------------|------------------------------------------------|
| `browser.py` | Chrome | CDP `GET /json/version` **verified against this cluster's Chrome** (profile argv token + LISTEN socket on the CDP port, both directions) **and** ava-browser session liveness | `respawn_service` (an orphan of ours is swept + rebuilt; skipped when another unit's browser holds the port — see Notes) | the supervised Chrome of this cluster actually serves CDP — two questions, because either alone lies |
| `browser_mcp.py` | MCP upstream | Unix socket protocol `ping` (JSON request, `ok` reply — the daemon's accept/read loop must answer) | `respawn_service` | the browser-MCP daemon's loop answers requests; lock-free by design (a slow browser op must not read as death) |
| `brew_pin.py` | Homebrew dependency pin policy | read-only `brew list --pinned`; non-macOS and hosts without brew are silent no-ops | none — one ERROR per drift episode tells the operator to run `brew pin <formula>` manually | every formula in the operator-approved manifest remains pinned; it never changes package state |
| `computer_mcp.py` | Computer-use service | Unix socket protocol `ping` (lock-free; does not take the action lock) | `respawn_service` | the computer-MCP daemon's accept/read loop answers |
| `mcp_daemon.py` | Shared MCP daemon | Unix socket protocol `ping` | `respawn_and_verify` (probe-confirmed) | the shared MCP daemon's loop answers |
| `delivery_watchdog.py` | Delivery watchdog | HTTP `/healthz` (identity-verified) with Liveness beat | `respawn_and_verify` | our daemon's work loop is still ticking (a wedged loop 503s via Liveness) |
| `im_bridge.py` | IM Bridge | HTTP `/healthz` (identity-verified); before respawn, a second bounded read accepts this unit's matching `name` + `home` even on a stale 503 or pidfile mismatch | `respawn_and_verify` | our daemon still owns and answers the health port; Liveness staleness is warning-only because an IM long poll can legitimately block the work loop |
| `heartbeat.py` | Heartbeat | HTTP `/healthz` (identity-verified) with Liveness beat | `respawn_and_verify` | our daemon's work loop is still ticking |
| `idle_shell_reminder.py` | Idle-shell-reminder | HTTP `/healthz` (identity-verified) with Liveness beat | `respawn_and_verify` | the daemon's work loop is still ticking |
| `labeler.py` | Labeler | HTTP `/healthz` (identity-verified) with Liveness beat | `respawn_and_verify` | our daemon's work loop is still ticking |
| `memory_indexer.py` | Memory-indexer | HTTP `/healthz` (identity-verified) with Liveness beat | `respawn_and_verify` | our daemon's work loop is still ticking |
| `events_maintenance.py` | Events-maintenance | HTTP `/healthz` (identity-verified) with Liveness beat | `respawn_and_verify` | our daemon's work loop is still ticking |
| `restarter.py` | Restarter | HTTP `/healthz` (identity-verified) with Liveness beat | `respawn_and_verify`, **then** a stand-in `RespawnController` pass if the respawn never verifies — see Notes | our daemon's work loop is still ticking; the stand-in path runs the daemon's own dispatch controller (real DB traversal) |
| `agent_host.py` | Agent host | HTTP `/healthz` (identity-verified) with Liveness beat | `respawn_and_verify` | our daemon's work loop is still ticking |
| `page_server.py` | Page-server supervisor | HTTP `/healthz` (identity-verified) with Liveness beat | `respawn_and_verify` | our daemon's work loop is still ticking |
| `ops.py` | Agent-ops | HTTP `/healthz` (identity-verified) — request-driven server, no periodic loop; the healthz answering at all proves the loop responsive | `respawn_and_verify` | the ops server answers this unit's identity on its port |
| `gateway.py` | Gateway | HTTP `/api/health` (pings Postgres — `SELECT 1` — so a gateway that can serve HTTP but cannot reach the DB reads unhealthy) + this unit's `home` (`shared.daemon_health.probe_home`) | `respawn_and_verify` | the gateway serves AND its database path works (the 0004 fix) |
| `frontend.py` | Next.js | HTTP 2xx on the **app** port (not the entry port — the entry's always-up gate answers 200 while the app is down) | kill session + `npm run build && npm run start` | the Next.js app renders (a dead app cannot answer 2xx) |
| `milvus.py` | Milvus | real RPC — `MilvusClient.list_collections` against the cluster's milvus URI (the indexer's own client path); a bare TCP connect was replaced because a port-open probe stays green while the server behind the port is unusable (issue #192) | `respawn_service` | milvus serves RPCs — the check breaks exactly when the indexer's calls would break |
| `pgbouncer.py` | per-cluster PgBouncer pooler | admin-console connect on the registry-derived listen port (client scram, no backend hop) — deliberately NOT the end-to-end `SELECT 1`, so a Postgres outage is not answered by restarting a healthy pooler | `ensure_pgbouncer` (the same idempotent bring-up `ava start` runs), verified, then a `pgbouncer_repaired` telemetry event | the pooler's own protocol answers (loopback AND the reachable address remote runners dial) |
| `redis_acl.py` | per-cluster redis ACL | PING as this cluster's username (read from `redis_identity()` using the cluster's `redis_url` — names-as-data, no longer derived from cluster name) | re-affirm ACL user | the cluster identity authenticates to redis — the exact path every component's redis call takes (the 0004 guardrail) |
| `otel_collector.py` | OTel collector sidecar | POST `/v1/traces` to the OTLP receiver must return 2xx, and the listeners on :4318/:8888 must resolve to this unit's collector binary + live session record; an unrecorded same-binary holder is stale/down, while another holder is terminal | give each verified stale holder one 5 s SIGTERM window before a verified SIGKILL fallback, then `respawn_and_verify` through the same 5 s session-stop ladder; a survivor remains loud/down | the supervisor-owned OTLP listener the agents export through answers, rather than an old collector that happens to retain the port |
| `lgtm.py` | local LGTM backends (`deploy/lgtm/`) | three readiness endpoints (Loki/Prometheus/Grafana) on their native fixed host ports; remote Tempo is excluded so its failure cannot restart local backends; any HTTP answer = alive, connection failure = down; no-op without the `$AVA_HOME/lgtm-host` marker | re-run the idempotent `deploy/lgtm/start.sh` | each local backend's readiness listener answers (its own health traversal) |

The roster is pinned to reality by `scripts/lint_doc_roster.py` (set equality against the module directory and the ServiceSpec + hand-added registrations) — a module added, removed, or renamed without updating this table fails the lint.

Audit 2026-08-21 (issue #192): all 22 checks present at the audit traversed what they certify. `milvus.py` was the one port-open-only probe and was upgraded to a real RPC; the phantom `task_maintenance` row and seven missing rows are fixed here. The later `brew_pin.py` assertion traverses Homebrew's own read-only pin roster; later additions follow the same traversal rule.

Parent: [[services/healthchecks/healthchecks.ava.okf.md|healthchecks]].
