---
type: doc
title: Restarter — Agent Restart Scheduler + Orphan Reaper
description: "Agent restart scheduler: RespawnController (restart+reap), HibernateController (swap out/in via SIGUSR1), CrashResurrectController (auto-resurrect involuntary deaths). Gateway-health gated, purely local."
tags: []
---

# Restarter — Agent Restart Scheduler + Orphan Reaper

## What It Is
Polls local `agents_meta` rows with `status='restarting'` every second and calls `respawn_agent`; also reaps local orphans whose pid no longer resolves to the agent's own process. Decoupled from Gateway, purely local.

**Role attribution**: agent-runner side (Gateway does not run it) — `ServiceSpec.capabilities=_AGENT_RUNNER` in `ops/spec.py`, roster = `services_for_capabilities` ∩ local `machine_role()`.

## Core Responsibilities
Each tick runs three machine-scoped, non-blocking (`blocks` always `BlockScope.NONE`) controllers: `RespawnController` (restart+reap), `HibernateController` (swap out/in), `CrashResurrectController` (auto-resurrect on crash).

### RespawnController (`ops/controllers/respawn.py`)
- **Poll restart requests**: every second SELECT local `status='restarting'` agents.
- **Gateway health gate** (#517): `_gateway_healthy()` false → skip the round; the row stays restarting, retries next round.
- **Process rebirth**: `ops.agents.respawn_agent` starts a new session (`ava-agent-<id>`, via `session_name` in `ops/agent_launch.py:188`) bound to the same agent_id; internally CAS race-safe.
- **Four orphan reapers** (machine-scoped, local rows only, pin pid/age to close the ABA window, 30s cadence, all set `termination_source='reaper'`).
  The pid predicate is process **identity**, not liveness (`ops/agent_identity.py:probe_agent_process` matches the row's pid
  against the agent's own launch argv): a recycled pid stays alive forever without being the agent, which stranded rows
  indefinitely (#1123). Reaps on `GONE`/`FOREIGN`; `UNREADABLE` (another user's process, a zombie) counts as resident —
  the safety floor. Cases:
  - `starting` with pid not the agent's own process → terminated (crashed during boot)
  - `allocated` stuck past `AVA_ALLOCATED_REAP_GRACE_SECONDS` (120s) → terminated (died before claim). The grace is the OUTER bound of the launch path: it must exceed `AVA_LAUNCH_CONFIRM_TIMEOUT_SECONDS` (45s) plus boot, and the launcher's live-child extension stops at exactly this grace so the two never contend for the row (`tests/shared/test_config.py` pins the ordering).
  - `running`/`idling` with pid not the agent's own process → terminated (silent OOM/SIGKILL, or a pid the OS recycled)
  - `running`/`idling` with an **expired or missing liveness lease** → collected by `_collect_local_lease_zombies` (a resident pid is force-killed; the revive pass then relaunches the row in place). The lease is the liveness authority — a row that stopped renewing is dead even when its pid still answers. Gated by a post-outage grace window (`AGENT_LEASE_ZOMBIE_GRACE_S`), so paused-but-alive agents get time to renew ([[agent/lease.ava.okf.md|Agent Liveness Lease]])
  - **No scan set includes `hibernating`** — swapped-out agents never harmed; invariant: `tests/services/test_hibernation.py::TestReaperImmunity`.

### HibernateController (`ops/controllers/hibernate.py`) — Memory Swap Out/In
- **Swap out** (gated on `AVA_HIBERNATE_ENABLED`): SELECT local `idling` agents with `now()-last_active_at > AVA_HIBERNATE_IDLE_THRESHOLD_SECONDS` (default 450s > heartbeat 300s), no pending inbound, non-null pid → identity-probe the pid, and only on `OWNED` `os.kill(pid, SIGUSR1)`.
  A `FOREIGN` pid is logged and skipped, never signalled: SIGUSR1's default disposition is *terminate*, so signalling a recycled
  pid aims a kill at an uninvolved process (#1123). The agent exits cleanly; the row stays `hibernating` (via `/hibernating` finalize — no page close, no exit events).
- **warm-pool floor** (`AVA_HIBERNATE_MIN_ACTIVE`, default 100): local `running`/`idling` ranked by `last_active_at` DESC; rank ≤ floor exempt; only beyond-floor agents are candidates. `hibernating`/`terminated` never count. 0 = no floor (unlimited swap-out).
- **Swap in** (**unconditional** — runs even when enabled=False): SELECT local `hibernating` with pending inbound → `ops.agents.swap_in_agent` (CAS `hibernating→allocated` + new process, **no lifecycle inbound**). The **unified wake-up path**: any source inserting pending inbound (heartbeat raw INSERT, chat, task expiry) is picked up by polling — sources never do their own swap-in.
- Both scans run every reconcile (1s) via one indexed SELECT (probe + `os.kill` only on hits). Swap-out is idempotent *when the signal lands*: the signaled agent leaves 'idling' within ms, so the next tick skips it. A pid that fails the identity probe draws no signal at all, and the reaper clears the row within its 30s pass — which is what ends the re-selection (#1123).

### CrashResurrectController (`ops/controllers/resurrect.py`) — Auto-Resurrect on Crash
- **Criteria**: local `terminated` agents with `termination_source ∈ {'reaper','launch-confirm'}` (involuntary death), pending inbound in the workload allowlist (`chat`/`compact_request`), past per-agent backoff (`auto_resurrect_backoff_seconds`; one UPDATE atomically claims + stamps `last_resurrect_at`) → `ops.agents.resurrect_agent`.
- **Never resurrect**: `'user'` (force-kill) / `'exit'` (self-exit) — explicit intent; `'integrity'` (row state self-inconsistent — ops looks first); NULL (pre-column legacy) — only post-change rows eligible, rollout-safe.
- **The allowlist is the whole safety model**, so the stamp is enforced, not just documented: an unstamped write leaves NULL, which this scan can never claim — the queued inbound strands silently. `scripts/lint_termination_source.py` fails any terminated-write that omits its source; `TerminationSource.resurrectable()` is what `_RESURRECTABLE_SOURCES` reads. `'launch-confirm'` also covers the child's early-boot schema/placement rejection (`agent/_starting.py`).
- **Why needed**: Gateway's `resurrect_if_terminated` fires only on new inbound; already-queued inbound after a crash gets no wake event → indefinite stall. This controller is the persistent fallback.
- **Gateway health gate**: same as RespawnController; unhealthy → postpone round (no backoff stamp, retry next round).
- 30s cadence; a single resurrection failure is only logged, the round continues.

## Key Dependencies
- [[db.ava.okf.md]] — reads/writes `agents_meta` (restarting requests + orphan status + termination_source)
- [[loop.ava.okf.md]] — respawn/resurrect result = agent process re-entering the main loop
- [[gateway-cli.ava.okf.md]] — probes gateway health endpoint before respawn/resurrect

## Entry Points
- `services/restarter/daemon.py` — `python -m services.restarter.daemon` (constructs the three controllers, reconciles each tick)
- `ops/controllers/hibernate.py:HibernateController` — swap out/in
- `ops/controllers/resurrect.py:CrashResurrectController` — auto-resurrect on crash
- Watchdog keepalive via `services/healthchecks/restarter.py` (HTTP `/healthz`)

## Notes
- Multi-machine: only acts on local machine (`machine=machine_name()`) — `respawn_agent`/`swap_in_agent` start local processes; cross-machine startup hits the boot placement gate (agent 1513).
- The reaper **pushes** orphans to terminated (visible + resurrectable); explicit resurrection still goes through gateway/ops.
- Hibernation is invisible to SDK/frontend (projected as idling); heartbeats still wake hibernating agents. See [[process-lifecycle.ava.okf.md]] + `decisions/2026-07-20-agent-hibernation.md`.
