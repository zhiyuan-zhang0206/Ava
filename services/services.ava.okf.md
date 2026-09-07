---
type: doc
title: Background Services
description: 'Ava background service collection — roster services run in session backends and are health-checked by watchdog; the macOS permissions-helper is a launchd-owned exception. Services are distributed by gateway / agent-runner capability.'
tags: []
---

# Background Services

## What It Is
Ava's background service collection—roster services are independent long-lived
processes hosted in sessions and restarted by a watchdog health check every 60
seconds. Agent interactive shells live in per-session pty hosts outside that
roster. On macOS, the permissions-helper is a
launchd-owned exception with its own keepalive and a nursery protocol for
idempotently spawning, listing, checking, and signaling named direct children,
plus in-place helper upgrades. Converge installs its app at the checkout-independent
`$AVA_HOME/helper/AvaPermissionsHelper.app`, rebuilds only when a content hash of
the Swift source, Info.plist, or expected signing requirement changes, and signs
with a designated requirement pinned to the named certificate's SHA-1 identity.
The agent-runner watchdog pings its protocol and reloads launchd after three failures.

## Grouped by Capability
Each service declares which machine capabilities it runs on (`ServiceSpec.capabilities` in `ops/spec.py`); a machine runs every service whose capability set matches. The groupings reflect the **merged roster** of core entries + entries contributed by `_plugin_services()`; node ownership follows the code—a service registered by a plugin has its node under that plugin's subtree. Sub-trees are split accordingly into two groups; services that run on both sides stay at this layer:

- [[gateway_side.ava.okf.md|Gateway-side services]] — `capabilities` includes `gateway`: heartbeat, im-bridge (the product frontend), delivery-watchdog, events-maintenance, milvus, memory-indexer, labeler, pg-backup; plus the gateway itself / frontend documented elsewhere, and the plugin-registered task-maintenance (node under the [[ava_builtins/plugins/ava_fleet/ava_fleet.ava.okf.md|ava_fleet plugin]] subtree)
- [[agent_runner_side.ava.okf.md|Agent-runner-side services]] — `capabilities` includes `agent-runner`: agent-host, agent-ops, browser, permissions-helper (plus browser-mcp, computer-mcp)
- **Both sides** (one instance each, stay at this layer):
  - [[watchdog.ava.okf.md|Watchdog]] — service health checks + schema/pin/pause self-healing; one instance per capability (`gateway-watchdog` + `agent-runner-watchdog`)
  - [[healthchecks.ava.okf.md|Healthchecks]] — health probes for each service; called by the corresponding capability's watchdog

## Core Responsibilities
- **Independent deployment**: each daemon starts via `.venv/bin/python -m services.<name>.daemon`, fully decoupled
- **session management**: the unified backend tracks one session per service. POSIX normally uses `shared/posixproc.py`; on macOS, enabling the default-off `AVA_PERMISSIONS_HELPER_SPAWN` alongside the helper routes services and pty hosts into direct helper children, with disk records as lifecycle truth. Agent shells/watchers remain [[shared/pty_sessions/pty_sessions.ava.okf.md|pty sessions]]; the helper itself remains launchd-owned
- **watchdog keep-alive + self-healing**: runs the corresponding healthcheck every 60s, restarts on death; additionally runs five reconcile controllers in order (`ops/manager.py:build_controllers`): updater (reaps hung ava-updater sessions, agent-runner only) → pause (stranded-pause recovery, 120s timeout) → schema (schema version) → pin (cluster pin; agent-runner self-heals, gateway only alerts) → code
- **Distributed by capability**: intersection of `ServiceSpec.capabilities` with the local machine's `machine_role()` determines which services run—gateway and agent-runner each run a different subset, each with its own watchdog instance

## Key Dependencies
- [[watchdog.ava.okf.md]] — keep-alive + self-healing scheduler for all services
- [[loop.ava.okf.md]] — agent-host/heartbeat/task-maintenance directly operate on agent/task lifecycles
- [[gateway-cli.ava.okf.md]] — gateway hosts heartbeat, im-bridge, delivery-watchdog, events-maintenance, task-maintenance, memory-indexer, milvus, labeler, pg-backup

## Entry Points
- `ops/spec.py:build_services()` (re-exported by cli `_repo.py`) — single source of truth for the service roster (session + probe metadata); core groupings + entries contributed by `_plugin_services()` from plugin declarations
- `services/watchdog/daemon.py` — service keep-alive + self-healing entrypoint
- `services/agent_host/daemon.py` — local agent turn scheduler

## Notes
- watchdog replaces OS cron (macOS Full Disk Access restrictions cause cron to hang in specific terminal contexts)
- The service keep-alive roster **single source of truth** = `build_services()`'s `ServiceSpec.healthcheck_module`: the watchdog derives from it (`_checks_for_capability`), no longer a second hardcoded list; registering a new service automatically gets 60s keep-alive. Manually attached pseudo-checks cover data plane (`redis-acl` / `pgbouncer`), host policy (`brew-pin`), permissions-helper, LGTM, and the remote station. pg-backup is a regular service.
- **browser profile provisioning**: the browser uses a dedicated profile `$AVA_HOME/chrome-profile` (isolated from the user's daily Chrome). On the first `ava start` with an interactive TTY and an absent profile, converge's `_ensure_browser` offers a choice via `services/browser/profile.py:ensure_browser_profile`: create a new empty profile (default) or copy from the user's daily Chrome profile (the agent directly inherits all login state and acts as the user—a security trade-off, requires explicit confirmation; excludes `Singleton*` locks + caches, refuses if Chrome is running). Any existing profile directory, including an empty or partial first copy, is never overwritten, cleared, or re-copied; non-interactive paths (watchdog/rollout/boot) always default to new without prompting. On macOS the daemon additionally waits for a GUI session plus a ready login Keychain before launching Chrome; the wait is reported as degraded and does not mutate `Local State`.
- **permissions-helper identity and updates**: `build-state.json` binds the stable installed app to its source hash and designated requirement; converge rejects identity drift and upgrades a running helper in place, preserving PID, grants, and nursery children; older helpers fall back to launchd kickstart. Converge and the debounced watchdog call `repair_unresponsive_helper()` for one bootout/bootstrap repair. A valid checkout-era bundle moves into `$AVA_HOME/helper` without rebuilding. Helper-routed spawns fail loudly when the helper is unavailable—never to `posixproc`. Host boot/heartbeat exit nonzero when their injected helper-parent marker no longer matches, allowing supervisor respawn.
- **plugins can also register services**: a plugin provides `plugins/<name>/services.py` exposing `services() -> tuple[ServiceSpec,...]`, `ops/spec.py:_plugin_services()` discovers based on code presence and folds them into `build_services()` (session name conflicts fail-fast). "Plugin declares, ops discovers," without a reverse dependency on plugin domain code. Discovery is based solely on code presence, not on the agent-side enable state (the roster is machine-facing); a service's own cluster-level gating uses `ServiceSpec.gate` reading explicit settings fields. task-maintenance lives under the [[ava_builtins/plugins/ava_fleet/ava_fleet.ava.okf.md|ava_fleet plugin]] in this way.
- roster encoding is **explicit per service**: each `ServiceSpec` declares its `capabilities` (gateway / agent-runner / both) and its **`requires_db`** — needs Postgres or not, so a DB-scoped watchdog block holds back only the DB's users ([[services/watchdog/block-scope/block-scope.ava.okf.md]]).
