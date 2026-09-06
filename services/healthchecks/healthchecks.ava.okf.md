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

**Role attribution**: both sides — not independent processes; each probe is called by its capability watchdog (`gateway-watchdog` / `agent-runner-watchdog`), derived from `ServiceSpec.healthcheck_module` or attached as a documented pseudo-check.

## Health Check List
The full roster — every checked module with its service, liveness probe, and restart method: [[services/healthchecks/check-roster/check-roster.ava.okf.md]].

## Core Responsibilities
- **Independent liveness logic**: each service defines its own "alive" criteria (HTTP `/healthz` or `/api/health` / CDP / unix socket ping / real RPC via `MilvusClient` / redis PING) — there are no longer any pidfile-probed or port-open-only services in the table
- **Identity before liveness**: every probe that CAN prove the answering process is this unit's does. The shared probe is also what `ava status` / `ava cluster health-probe` / `ava start`'s readiness gate run, via `ServiceSpec.identity_probe`; im_bridge's watchdog adds one local safety veto after that verdict (documented below). ([[services/healthchecks/terminal-verdict/terminal-verdict.ava.okf.md]])
- **Restart trigger**: `run_keepalive` rebuilds a session after a respawnable failure; a failed respawn backs off exponentially, and past `watchdog_respawn_breaker_rounds` non-alive rounds the breaker holds with one `respawn_breaker_open` alert (task #1941); terminal verdicts report
- **Frontend special handling**: Next.js has no PID hook; probed via HTTP curl, restart = kill session + `npm run build && npm run start` (30-60s)
- **Six hand-added pseudo-checks have no ServiceSpec**: `brew-pin` covers host policy; agent-runner `permissions-helper` covers the launchd app; `pgbouncer` / `redis-acl` cover native data-plane processes; `lgtm` covers native backends through readiness plus a Loki write/read probe; gateway `station-probe` covers the remote observatory. Only station-probe requires DB (to resolve `machine_units`). Pgbouncer probes its loopback admin console plus the configured reachable-address listener (`pgbouncer_public_listener_reachable`): it can stay alive after one bind fails, so loopback alone can look green while its public front door is down.
- **permissions-helper exception**: its long-running watchdog keeps episode state in memory, pings the real protocol with a 3 s response timeout, and calls converge's `repair_unresponsive_helper()` on the third failed round. Only a later healthy round resets the episode.
- **redis-acl exception**: PING failure distinguishes a reachable server that lost its in-memory ACL (replay `ensure_cluster_redis_acl`) from an unreachable local Redis (reuse `_start_redis`, then verify PING); no-secret clusters skip ACL re-affirmation but still respawn dead Redis (gateway-only, runs every 60 seconds)

## Key Dependencies
- [[services/watchdog/watchdog.ava.okf.md]] — sole caller (gateway / agent-runner each run the batch for their own capability)
- `shared.service_respawn.respawn_service` — unified restart entry

## Notes
- **A probe always returns a verdict, never an exception** — enforced by a total wrapper per probe, not by widening catch tuples, and failing closed. Also covers browser's two-probe rule, redis-acl's documented exception, and the same rule generalized to the whole `main()` (nothing failable between the dead verdict and the respawn): [[services/healthchecks/probe-contract/probe-contract.ava.okf.md]].
- **Not every dead verdict is respawnable** — a `/healthz` answered by another unit's daemon is terminal: reported at ERROR and NOT respawned, because no session can free its port. The collector follows the same rule: it reclaims only an identity-verified same-binary listener absent from its live record. The three-way verdict and where the line falls: [[services/healthchecks/terminal-verdict/terminal-verdict.ava.okf.md]]. `shared.service_respawn.run_keepalive` applies the same policy to the service keepalives, including pg-backup and pitr-uploader.
- **im_bridge port-holder veto** — after a respawnable shared verdict, its watchdog re-reads the same `/healthz` with the shared five-second probe bound. Matching `name="im_bridge"` + this unit's `home` is returned as alive with a WARNING even when the response is 503-stale or its pid differs from the pidfile: long IM polls make loop staleness insufficient evidence for killing the daemon. Unreadable, non-Ava, and mismatched identities preserve the shared verdict and existing action. The operator surfaces intentionally retain the stricter shared probe verdict.
- browser_mcp's ping deliberately avoids an upstream round trip or daemon serial lock — slow browser operations could hold the lock longer than the probe timeout, causing false kills
- The frontend healthcheck uses HTTP `curl localhost:3000` (`npm run start` doesn't expose a PID); it's **truly** special in its **restart method** — kill session + `npm run build && npm run start` (build gate), not in its probing method (gateway uses `/api/health`, browser uses CDP, milvus uses a real RPC via `MilvusClient`, browser_mcp uses socket, redis_acl uses PING — none of those are `/healthz`)
- **Traversal audit (issue #192, 2026-08-21)**: every check must traverse what it certifies — a check that shares no code path with what breaks stays green through the outage (the 0004 lesson; `conventions/defensive-patterns.md`). The audit found the rule respected by 20 of the then-21 checks; `milvus.py` was the one port-open-only probe (a bare TCP connect) and was upgraded to a real `list_collections` RPC. Later `brew_pin.py` and `permissions_helper.py` checks read the package manager and helper protocol they respectively certify. The roster table carries a "what it certifies" column, and `scripts/lint_doc_roster.py` pins the table to the module directory + ServiceSpec + hand-added registrations (set equality) so the drift found in the audit cannot silently return.
- A healthcheck module is invoked ⟺ some `build_services()` `ServiceSpec.healthcheck_module` declares it (the hand-added pseudo-checks above are the exception) — watchdog derives the roster **single-source** from that field ([[services/watchdog/watchdog.ava.okf.md]]). Merely placing a module in `services/healthchecks/` won't cause it to be called. A healthcheck module **does not have to** live in this directory: `healthcheck_module` is just an importable dotted module string; services registered by plugins place their healthchecks in their own namespace (e.g., `ava_builtins.plugins.ava_fleet.task_maintenance.healthcheck` is declared by the `task-maintenance` ServiceSpec, and watchdog still pulls it in via importlib and keeps it alive every 60s).
