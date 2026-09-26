---
type: doc
title: Gateway-Side Services — background services running with gateway capability
description: "Groups background daemons running on machines (single-machine or gateway-only) whose capabilities include gateway — data-plane-adjacent services: cluster-level wake-up daemons like heartbeat/task-reminder, vector memory stack, auto-labeling, daily backup. The source of truth is `ServiceSpec.capabilities` in `ops/spec.py`."
tags: []
---

# Gateway-Side Services

## What is it
Groupings of background services running on machines whose capabilities set includes `gateway` (single machine with `gateway,agent-runner` or pure gateway). The gateway capability owns data-plane-adjacent daemons: the HTTP gateway itself, frontend, cluster-level wake-up daemons (heartbeat / task-maintenance only INSERT inbound + Redis best-effort publish to wake the owner; the agent's SELECT recheck guarantees delivery, so they belong to a single gateway, not every runner), memory/vector stack, all selected into the host root manifest.

`services/gateway_side/` is a **capability grouping, not a directory of code** — there is no `services/gateway_side/*.py`. Each daemon's code lives in its own `services/<name>/`; which side it runs on is a `ServiceSpec.capabilities` attribute, which cuts across the filesystem and cannot be expressed by co-location. This node and its children are the index layer for that attribute, the same way the domain roots under `okf/` are the index layer for the whole graph. Do not "fix" it by flattening the children into `services/` — that deletes the only place the capability split is represented in the hierarchy.

## Service List
Source of truth = services in `build_services()` of `ops/spec.py` whose `ServiceSpec.capabilities` include `gateway` (the `_GATEWAY` / `_BOTH` groups), including plugin-declared entries folded in via `_plugin_services()` — this table is the **merged roster**'s gateway-side view.

| Service | Responsibility | File |
|---------|----------------|------|
| heartbeat | Idle agent heartbeat wake-up | [[heartbeat.ava.okf.md]] |
| im-bridge | **Product frontend** — every IM channel adapter (Telegram / WeChat / Feishu), dialog push + commands | [[im_bridge.ava.okf.md]] |
| delivery-watchdog | Wake dispatch + stall alerting + terminated-owner resurrect retry + stale-inbound dead-letter sweeps + stalled crash-marked recovery request + hosted-turn liveness recovery | [[delivery_watchdog.ava.okf.md]] |
| events-maintenance | unified `events` stream maintenance (immutable-Loki class resolution + day-grain rollup) | [[events_maintenance.ava.okf.md]] |
| milvus | Vector database (memory-indexer backend) | [[milvus.ava.okf.md]] |
| memory-indexer | memory pool vector index | [[memory_indexer.ava.okf.md]] |
| labeler | agent auto-naming | [[labeler.ava.okf.md]] |
| pg-backup | Daily local Postgres backup (scheduler daemon, ServiceSpec service) | [[services/gateway_side/backup/backup.ava.okf.md]] |
| pitr-uploader | Disabled-by-default encrypted immutable GCS WAL uploader | [[services/pitr/pitr.ava.okf.md]] |

## Also Owned by Gateway Capability, Documented Elsewhere
The following services also run on the gateway capability (core `_GATEWAY` entries or plugin-folded-in entries), but their concept docs live in their own subtrees and are not repeated here:

- **gateway** — HTTP gateway itself (see [[gateway/gateway.ava.okf.md]])
- **task-maintenance** — overdue task reminder + escalation. Registered by the `ava_fleet` plugin via `ava_builtins/plugins/ava_fleet/services.py`, folded into the roster by `ops/spec.py:_plugin_services()`; runs on the gateway side, but **the node lives with its code in the plugin subtree** (see [[ava_builtins/plugins/ava_fleet/task_maintenance/task_maintenance.ava.okf.md|ava_fleet › Task Maintenance]])
- **frontend** — Next.js frontend (`ui/web/` subtree)
- **gate** — the public fleet UI entry, an ordinary gateway `ServiceSpec` owned by root. It uses the `frontend` entry-port slot and proxies the Next.js app on the separate `app` slot. Root readiness checks `GET /__ava/healthz` and the captured listener lineage; status shows Gate in the ordinary service table. Gate stops with its selected application tree during planned downtime. See [[services/gate/gate.ava.okf.md|Gate authentication and maintenance projection]].

## Key Dependencies
- [[services/ava_root_glue/ava_root_glue.ava.okf.md]] — root probes and supervises the selected service manifest.
- [[services/services.ava.okf.md|Background Services Overview]] — upper-level index of grouping and capability distribution
