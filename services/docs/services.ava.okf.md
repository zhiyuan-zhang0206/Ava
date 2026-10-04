---
type: doc
title: Background services
description: Capability-selected services owned by one root supervisor, with explicit native resource and macOS permission boundaries.
tags:
- services
---

# Background services

One root supervisor owns each host's selected application-service tree.
`ops.roster.build_services()` merges core and plugin `ServiceSpec` declarations;
capabilities and service gates select the installed host's manifest. Each entry
must provide a readiness probe. A matching HTTP response is insufficient without
native ownership of the responding listener.

- [[services/docs/gateway_side/gateway_side.ava.okf.md|Gateway services]] include gateway, Gate, frontend,
  heartbeat, messaging, delivery-watchdog, ttl-reaper, schedule-manager, maintenance, indexing, and backups.
- [[services/docs/agent_runner_side/agent_runner_side.ava.okf.md|Agent-runner services]] include agent-host,
  ops, page-server, browser, and local MCP services.
- [[services/supervision/ava_root_glue/docs/ava_root_glue.ava.okf.md|Root deployment wiring]] registers
  the exact selected roster, service recovery, read-only diagnostics, and tree
  self-check. Delivery-watchdog is a business delivery service, not a service
  lifecycle scheduler.
- [[services/supervision/healthchecks/docs/healthchecks.ava.okf.md|Protocol probes]] return evidence;
  they have no spawn, session restart, or OS-job authority.

Code lives in domain groups under `services/<group>/<service>/` (no `__init__.py` on a group): `supervision` (root supervisor, its wiring, protocol probes), `agent_runner` (agent host, ops server, page server, pty sessions), `desktop` (browser, computer-use, permissions helper), `entrypoints` (Gate, IM bridge), `wake` (heartbeat, delivery watchdog, schedule manager), `upkeep` (TTL reaper, events maintenance), `derived` (memory index and search, labeler, hierarchy worker) and `backup` (scheduler, WAL-G, artifact handling, the dump itself). `redis_bridge` and `pidfile.py` stay at the top. The capability split documented below cuts across these groups.

Agent shells, watchers, and other persistent interactive sessions are subordinate
runtime work, rather than a second service lifecycle. On macOS the installed
permissions helper is root's permission-carrying ancestor; changing that ancestor
requires an external transition after native custody is settled. Linux has no
helper ancestor. Postgres, Redis, and PgBouncer are separately owned native data
resources and can remain available while application services are stopped for
maintenance.

Service restart is requested through root and verified with the same readiness
contract used by status and start. Unknown ownership never authorizes a kill or
spawn. Update, pause-hold completion, package acquisition, and native data-plane
transitions remain explicit lifecycle operations outside the service observer.
