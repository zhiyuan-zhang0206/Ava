---
type: doc
title: Watchdog — Service Liveness Monitoring + Self-Healing
description: One service healthcheck scheduler per capability — runs all healthchecks for the corresponding capability every 60s, restarts if dead, and before running healthchecks passes five reconcile gates (hung updater / stranded-pause / schema version / cluster pin / running code). Replaces OS cron, runs in a supervised session.
tags: []
---

# Watchdog — Service Liveness Monitoring + Self-Healing

## What it is
One service healthcheck scheduler per capability — runs all healthchecks for the corresponding capability every 60s, restarts on failure. Additionally, before running healthchecks each tick, passes five reconcile gates to self-heal the node back to cluster desired state. Replaces OS cron (unreliable under macOS Full Disk Access restrictions), runs in a supervised session, cross-platform.

**Role home**: one instance on each side — `gateway-watchdog` (gateway side) + `agent-runner-watchdog` (`ServiceSpec.capabilities=_AGENT_RUNNER`), each `--role` scoped to only self-heal services of that capability; a single machine running gateway,agent-runner runs both.

## Five reconcile gates (at tick start; a hit skips the healthchecks its reason applies to)
1. **hung updater** (`ops/controllers/stalled_updater.py`) — a live `ava-updater` session whose log has not advanced in `NO_PROGRESS_TIMEOUT_S` (15min) is force-killed. **agent-runner** only (the `ops` daemon that spawns those sessions is agent-runner-scoped, so the two are always co-resident; a gateway-only unit has none to reap). **Runs first, and never blocks** — a hung updater leaves the host paused, so a reaper behind gate 2 would be short-circuited away in exactly the case it exists for, and gate 5 defers to that same dead session for as long as it lives. (Gates 2 and 4 do not: they gate on the update *lease*, which a watchdog-spawned `ava-updater` never takes, so an off-pin host still reaches `spawn_update`'s caller-side reap on its own.) The same reap also runs caller-side inside `spawn_update`; that alone is not enough, because a live session makes `current_orchestration()` answer `"update"`, which `ops/deploy_window.py` signal 3 reports to the gateway and which refuses the cluster's next `ava update` **before** Phase B can fan out to the reap. Killing the session takes nothing down — it hands the host back to gates 2/4/5.
2. **paused posture** (`host_deploy_state.posture` = paused/converging) — written at service shutdown (and by the updater's lease claim), skip if hit so healthchecks cannot revive stopped services. The native maintenance journal also gates healthchecks throughout drain; it has no expiry and stranded-pause recovery cannot release it. **Legacy stranded-pause recovery**: a pause with no owner — no *executing* update lease AND no live local updater lease — for over `STRANDED_PAUSE_TIMEOUT_S` (2m) → judges the orchestration hard-killed and unpauses itself. An OWNED pause is declined however long it runs; its owner states end on their own (the lease TTL, or the stalled-updater reaper killing a silent session). Reading only the cluster lease missed a watchdog-spawned updater entirely, which is why the bound had to be 10min and why a failed updater's pause outlived it (issue #1074). The lease is asked `DeployLease.awaits(machine_name())`, not "is it held": a **settle hold naming this host** is not ownership — nothing runs under one, and it exists only because the orchestration proved this pause lost its owner. Deferring deadlocked the very host it waited for, since this gate blocks `ALL` and the gates that converge it never ran (issue #1116).
3. **schema version reconcile** — DB applied vs local code required: code behind DB (`CodeBehindSchema`/`MigrationLayoutError`) → triggers `ava update` self-heal; DB behind code (`SchemaVersionMismatch`) → only logs + waits for gateway migration; an unreachable DB → log + skip.
4. **pin drift** — HEAD ≠ cluster pin (`cluster_target_sha`). Capability division: **agent-runner** watchdog acts (force-update to pin, with persistent backoff to prevent infinite loop on bad pin); **gateway** watchdog only alerts (gateway drift requires full rollout, not single-node self-heal).
5. **running code** (`ops/controllers/code.py`) — HEAD **is** the pin but the commit this process froze at boot (`shared/process_sha.py`) is not: the checkout landed, the services were never replaced (`code ⚠` beside a green `pin ✓`), and no controller owned that state until a human noticed (2026-07-28). The heal is a **restart**, not a checkout: `spawn_update(restart_only=True)`, locally. **agent-runner** only; defers while the update lock is held or a local orchestration session is alive (mid-rollout it is the normal transient), and never acts outside the prod source tree. Same persistent backoff + shared cooldown as gate 4.

A gate blocks by **scope**: how wide its finding is, matched against each service's `ServiceSpec.requires_db` — [[block-scope.ava.okf.md]].

## Core responsibilities
- **Dispatch by capability**: `--role gateway` monitors gateway services, `--role agent-runner` monitors agent-runner services; single machine gateway,agent-runner runs two instances (two sessions, two pidfiles).
- **Healthcheck every 60s**: imports and sequentially runs the corresponding healthcheck modules, failure → kills the session → re-spawns.
- **First tick delayed one interval**: so services started sequentially by `ava start` have time to warm up, avoiding false-kill of a just-started gateway.
- **Watchdog freshness**: each capability binds its own per-unit `/healthz`
  listener (`gateway_watchdog` / `agent_runner_watchdog`) and advances its
  liveness only after a complete round. The endpoint includes
  `last_tick_at`, while the `watchdog_tick` ObservableGauge provides the same
  completed-round timestamp to Prometheus. A 90-second round deadline logs an
  error and leaves both signals stale rather than claiming that a wedged
  controller or healthcheck made progress. A deadline-detached round remains
  the sole in-flight round; later cadences skip it until its synchronous work
  returns, preventing concurrent reconciles or healthchecks from racing.

## Healthcheck checklist
The derived roster, hand-added pseudo-checks, capability gates, and exact
per-role order: [[services/watchdog/checklist.ava.okf.md]].

## Key dependencies
- [[healthchecks.ava.okf.md]] — each service corresponds to a healthcheck module
- [[gateway-cli.ava.okf.md]] — schema/pin self-heal via `POST /api/cluster/update`

## Entry points
- `services/watchdog/daemon.py` — `.venv/bin/python -m services.watchdog.daemon --role gateway`

## Notes
- **watchdog itself IS monitored — by the OS scheduler**: [[services/watchdog/os-monitoring.ava.okf.md|OS-level probe]] revives a dead pidfile every 60s; registered by converge, gated by `AVA_OS_JOBS_ENABLED`.
