---
type: doc
title: Task-Maintenance — Task Reminders + Escalation
description: A gateway-owned task reminder daemon — every 5 minutes scans in-progress tasks that are overdue for updates, sends a chat reminder to the owner; after accumulation reaches a threshold, escalates to the parent task owner, or if parent owner is empty (top-level task under root), inserts a require_response notice into the human queue instead. Only speaks, never modifies task status.
tags: []
---

# Task-Maintenance — Task Reminders + Escalation

## What It Is
A gateway-owned daemon — every `AVA_TASK_MAINTENANCE_INTERVAL_SECONDS` (default 5 min) scans `agent_tasks`, chat-reminds owners of in-progress tasks overdue for updates, and escalates repeat non-responders: with a delegator (parent task has an owner) notify the parent owner; top-level tasks (parent = unowned system root) escalate to the human queue via a require_response notice on the stuck owner. Cluster-level, one per cluster, same shape as heartbeat. **Only sends messages / inserts notices, never modifies task status**—no stale sweep, no auto-cancel, no orphan release.

**Attribution**: fleet domain service under the `ava_fleet` plugin namespace (not hardcoded in core `ops/spec.py`): the plugin declares a `ServiceSpec` via `ava_builtins/plugins/ava_fleet/services.py:services()`, `ops/spec.py:_plugin_services()` discovers it into the single-source `build_services()` roster—"plugin declares, ops discovers"; gateway capability. **Discovery keys on the plugin code's existence, not the agent-side enable-state**; the cluster switch is the explicit settings field `AVA_TASK_MAINTENANCE_ENABLED` (via `ServiceSpec.gate`, evaluated at daemon-start, unaffected by per-agent overlays).

## Core Responsibilities
- **Remind**: SELECT `status='in_progress' AND owner IS NOT NULL AND NOT is_root` and `now()-updated_at > remind_interval_seconds`; deliver a `chat` reminder via gateway `POST /api/agents/{id}/messages` (`source='system'`); on success `last_reminded_at=now()`, `reminder_count += 1`. `last_reminded_at` gates the backoff (`AVA_TASK_REMINDER_BACKOFF_SECONDS`, default 3600s)—at most one reminder per backoff within the same overdue window (so the count can climb to the escalate threshold in one window).
- **Escalate**: fires once when `reminder_count` reaches `AVA_TASK_ESCALATE_N` (default 3). Two paths: **with delegator** (parent owner non-null) → "child task owner unresponsive" chat to the parent owner; **without delegator** → INSERT a require_response `agent_notices` on the stuck owner (`task_id`=that task, `priority` inherited, "stuck, please reassign or cancel"). The notice is attached to the agent but **audience is human**—it rides the snapshot's `notices_awaiting_response` into the human queue. Delegator path sends at `== threshold` once; the user path uses `>=` (an already-open notice **skips the round and retries next sweep**, so slipping past the threshold never permanently misses the window), and the sent notice itself is the idempotency marker, keeping "at most one open notice per agent".
- **Auto-resurrect terminated owner**: reminders go **through the gateway delivery path** (`resurrect_if_terminated`), not a raw INSERT—a raw INSERT only publishes a Redis wake (a dead process has no subscription, so it would never resurrect). Terminated owners are therefore not excluded.
- **Schema drift suicide**: `psycopg.ProgrammingError` (code↔DB drift) directly exits, relying on healthcheck to restart, not retrying to mask the issue.

## Key Dependencies
- [[db.ava.okf.md]] — reads `agent_tasks` (SELECT overdue tasks, JOIN parent for `parent_owner` + task `priority`) + writes `last_reminded_at`/`reminder_count` counts + user escalation path writes `agent_notices` (require_response, then `publish_agent_updated_sync` refreshes snapshot so the queue updates live)
- [[../../../../gateway/gateway.ava.okf.md]] — reminder/escalation messages delivered via `POST /api/agents/{id}/messages` (with cluster secret auth), obtaining auto-resurrect
- [[loop.ava.okf.md]] — reminder/escalation messages are received and processed by the owner/parent owner on their respective machines

## Entry Points
- `ava_builtins/plugins/ava_fleet/services.py:services()` — declares this service's `ServiceSpec` (session `task-maintenance`, gateway capability, carries `gate` reading `AVA_TASK_MAINTENANCE_ENABLED`)
- `plugins/ava_fleet/task_maintenance/daemon.py` — `.venv/bin/python -m plugins.ava_fleet.task_maintenance.daemon`
- Healthcheck `plugins/ava_fleet/task_maintenance/healthcheck.py` (HTTP `/healthz` :8108 liveness → `shared.service_respawn.run_keepalive`)

## Notes
- Configuration gate `AVA_TASK_MAINTENANCE_ENABLED` (default on): turning it off disables reminders for the whole cluster. The gating logic (`gate`) travels with the plugin's `ServiceSpec`, not hardcoded in `ops/spec.py:_gate_reason`; operational configuration (interval/backoff/escalate_n + pidfile + port 8108) remains in global `shared.config` / `shared.daemon_health`, isomorphic with other gateway daemons.
- After daemon death, the gateway watchdog keeps it alive every 60s as a fallback: the list is derived from `ServiceSpec.healthcheck_module` in `build_services()` [[services/watchdog/watchdog.ava.okf.md|single-source derivation]] (plugin-registered entries are also covered); this service has been wired (healthcheck_module = `plugins.ava_fleet.task_maintenance.healthcheck`). Hence the "schema drift suicide" above works — after exit it gets resurrected next round.
- Difference from [[services/gateway_side/heartbeat.ava.okf.md]]: heartbeat wakes idle agents to work, task-maintenance reminds overdue tasks; both only INSERT inbound, both are one per cluster on gateway.
