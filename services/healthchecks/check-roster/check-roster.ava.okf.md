---
type: doc
title: Health probe module roster
description: Protocol helpers registered by service specs and root diagnostics.
tags:
- services
- healthchecks
---

# Health probe module roster

Application service probes are bound to captured root ownership by their roster
entry. Helpers in this table do not own recovery. Readiness and diagnostic
registration are distinct; the root manifest determines which services run.

<!-- lint:healthcheck-roster-table -->

| Module | Role | Protocol evidence |
|---|---|---|
| `agent_host.py` | Service protocol probe | Agent host loop health |
| `browser.py` | Service protocol probe | Browser CDP response and profile facts |
| `browser_mcp.py` | Service protocol probe | Browser MCP protocol ping |
| `browser_reach.py` | Read-only diagnostic helper | Temporary browser fetch contrasted with host request |
| `computer_mcp.py` | Service protocol probe | Computer MCP protocol ping |
| `delivery_watchdog.py` | Service protocol probe | Delivery progress health |
| `events_maintenance.py` | Service protocol probe | Maintenance loop progress |
| `frontend.py` | Service protocol probe | App HTTP response and captured root listener generation |
| `gate.py` | Service protocol probe | Gate entry HTTP health, independent of app availability |
| `gateway.py` | Service protocol probe | Gateway serving and database query health |
| `heartbeat.py` | Service protocol probe | Heartbeat loop progress |
| `im_bridge.py` | Service protocol probe | IM bridge loop health |
| `labeler.py` | Service protocol probe | Labeler loop progress |
| `lgtm.py` | Read-only diagnostic helper | Native backend protocol helpers and Loki write/read round trip |
| `mcp_daemon.py` | Service protocol probe | Shared MCP protocol ping |
| `memory_indexer.py` | Service protocol probe | Indexer loop progress |
| `memory_search.py` | Service protocol probe | Real exact-search POST |
| `milvus.py` | Service protocol probe | Milvus collection-list RPC |
| `ops.py` | Service protocol probe | Ops loop health and operation age |
| `otel_collector.py` | Service protocol probe | OTLP request accepted and both listeners in captured root lineage |
| `owned_service.py` | Read-only diagnostic helper | Generic TCP protocol and connected Unix-peer ownership envelope |
| `page_server.py` | Service protocol probe | Page-server loop health |
| `permissions_helper.py` | Read-only diagnostic helper | Parent helper ping and launchd failure classification |
| `pg_backup.py` | Service protocol probe | Backup progress and last-success age |
| `pitr_base_backup.py` | Service protocol probe | Base backup progress |
| `pitr_uploader.py` | Service protocol probe | Upload progress and disk footprint |
| `prod_venv.py` | Read-only diagnostic helper | Bounded dependency check and isolated import smoke |
| `redis_acl.py` | Read-only diagnostic helper | Runtime-credential Redis PING |

`owned_service.py` supplies the native ownership envelope for endpoints without
Ava identity payloads and for centrally wrapped HTTP/TCP specs. A matching home,
profile, executable, or successful protocol by itself is insufficient.

The [[services/ava_root_glue/diagnostics.ava.okf.md|diagnostic roster]] documents
platform/capability gates, per-check budgets, native data-plane custody, and
reporting. [[services/healthchecks/check-roster/roster-notes.ava.okf.md|Roster ownership]]
describes the documentation guard.
