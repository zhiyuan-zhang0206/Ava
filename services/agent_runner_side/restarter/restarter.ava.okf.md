---
type: doc
title: Restarter — Agent Restart Scheduler + Orphan Reaper
description: "Agent restart scheduler: RespawnController (restart+reap), CrashResurrectController (auto-resurrect involuntary deaths). Gateway-health gated, purely local."
tags: []
---

# Restarter — Agent Restart Scheduler + Orphan Reaper

## What It Is
Polls local `agents_meta` rows with `status='restarting'` every second and calls `respawn_agent`; also reaps local orphans whose pid no longer resolves to the agent's own process. Decoupled from Gateway, purely local.

**Role attribution**: agent-runner side (Gateway does not run it) — `ServiceSpec.capabilities=_AGENT_RUNNER` in `ops/spec.py`, roster = `services_for_capabilities` ∩ local `machine_role()`.

## Core Responsibilities
Each tick runs three machine-scoped, non-blocking (`blocks` always `BlockScope.NONE`) controllers: `RespawnController` (restart+reap), `CrashResurrectController` (auto-resurrect on crash), and `WedgedAgentController` (kill + resurrect live agents that stop consuming pending work).

**Serving boundary**: every automatic process launch holds the local
`shared.start_serving` generation lock and proceeds only after `ava start` has
completed fully ready. Corpse and terminated-zombie cleanup still runs while it
is closed; a failed or waived-unready start therefore cannot revive an agent,
while the next successful start preserves normal crash and reboot recovery.

### RespawnController (`ops/controllers/respawn.py`)
- **Poll restart requests**: every second SELECT local `status='restarting'` agents.
- **Serving + gateway health gates** (#517): before serving, leave a restart row
  unchanged; once serving, `_gateway_healthy()` false also skips the round. Either
  deferral keeps the row restarting for the next round.
- **Process rebirth**: `ops.agents.respawn_agent` starts a new session (`ava-agent-<id>`, via `session_name` in `ops/agent_launch.py:188`) bound to the same agent_id; internally CAS race-safe.
- **Four orphan reapers** (machine-scoped, local rows only, pin pid/age to close the ABA window, 30s cadence, all set `termination_source='reaper'`).
  The pid predicate is process **identity**, not liveness (`ops/agent_identity.py:probe_agent_process` matches the row's pid
  against the agent's own launch argv): a recycled pid stays alive forever without being the agent, which stranded rows
  indefinitely (#1123). Reaps on `GONE`/`FOREIGN`; `UNREADABLE` (another user's process, a zombie) counts as resident —
  the safety floor. Cases:
  - `running`/`idling` with a dead pid and no produced message → terminated (crashed during boot), then crash-resurrect backoff
  - unclaimed `idling` stuck past `boot_reap_grace_seconds` (120s; configured through the retained `AVA_ALLOCATED_REAP_GRACE_SECONDS` alias) → terminated (died before claim). The grace is the OUTER bound of the launch path: it must exceed `AVA_LAUNCH_CONFIRM_TIMEOUT_SECONDS` (45s) plus boot, and the launcher's live-child extension stops at exactly this grace so the two never contend for the row (`tests/shared/test_config.py` pins the ordering).
  - `running`/`idling` with pid not the agent's own process → terminated (silent OOM/SIGKILL, or a pid the OS recycled)
  - `running`/`idling` with an **expired or missing liveness lease** → collected by `_collect_local_lease_zombies` (a resident pid is force-killed; the revive pass then relaunches the row in place). The lease is the liveness authority — a row that stopped renewing is dead even when its pid still answers. Gated by a post-outage grace window (`AGENT_LEASE_ZOMBIE_GRACE_S`), so paused-but-alive agents get time to renew ([[agent/lease.ava.okf.md|Agent Liveness Lease]])

### CrashResurrectController (`ops/controllers/resurrect.py`) — Auto-Resurrect on Crash
The 30s auto-resurrect scan — criteria, never-resurrect set, enforced stamp, health gate: [[services/agent_runner_side/restarter/crash-resurrect.ava.okf.md]].

## Key Dependencies
- [[db.ava.okf.md]] — reads/writes `agents_meta` (restarting requests + orphan status + termination_source)
- [[loop.ava.okf.md]] — respawn/resurrect result = agent process re-entering the main loop
- [[gateway-cli.ava.okf.md]] — probes gateway health endpoint before respawn/resurrect

## Entry Points
- `services/restarter/daemon.py` — `python -m services.restarter.daemon` (constructs the three controllers, reconciles each tick)
- `ops/controllers/resurrect.py:CrashResurrectController` — auto-resurrect on crash
- Watchdog keepalive via `services/healthchecks/restarter.py` (HTTP `/healthz`)

## Notes
- Multi-machine: only acts on local machine (`machine=machine_name()`) — `respawn_agent`/`revive_agent` start local processes; cross-machine startup hits the boot placement gate (agent 1513).
- The reaper **pushes** orphans to terminated (visible + resurrectable); explicit resurrection still goes through gateway/ops.
- Hibernation was deleted (2026-08-30): see `decisions/2026-07-20-agent-hibernation.md` for the historical design.
