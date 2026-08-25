---
type: doc
title: Cluster Rollout Boundary
description: The process, readiness, recovery, and Phase-B boundaries that keep agents stopped until a new gateway can serve them.
tags:
- cli
- operations
- rollout
---

# Cluster Rollout Boundary

## Process and recovery boundary

- An executing deploy lease refuses an operator `ava start` before migrations.
  For an internal start, only a gateway-capable fresh child inherits the
  rollout parent's boundary: it suppresses the restarter and leaves posture
  `converging`. A pure agent-runner Phase-B child completes its host-local
  transition normally — restarter launched, posture `idle` — so the gateway's
  Phase-B poll can observe it as converged.
- Failed-update schema recovery is the narrow exception to runtime-owner DB
  authentication: it targets this cluster's database through the gateway's
  local Postgres trust socket, so a partially applied credential transition
  cannot prevent last-known-good rollback. Manual `ava cluster rollback` keeps
  using the runtime owner connection.

## Readiness and Phase B

- `update.py`'s Phase-B poll answers a `PollVerdict` per host — the stall verdict
  reads the host's `host_deploy_state` row (idle → OK; live lease → working;
  paused+expired / converging+no-lease → STALLED ×2). A POLL_* status plus the
  `last_updater_outcome` the runner reported on the probe that settled it
  (`ops.updater_outcome`, read off that host's own updater log and anchored to
  its pause flag so a previous update's log is reported as *no record* rather
  than as this one's; on Windows, where the supervisor appends every run to ONE
  log, that flag anchors twice — the updater echoes a per-run start marker and
  the tail is sliced at it, so a previous run's decline is not read as this
  run's verdict). The status alone stops one level short of what an operator
  needs: `POLL_STALLED` covers both a preflight that refused (nothing stopped,
  host still serving its old code) and an updater that died after moving the
  checkout, and those want opposite next actions. It carries no extra dial —
  the probe that proves the host stopped is the probe that says why — and
  changes no deploy behaviour. A refusal is told to re-run rather than to wait:
  its own watchdog cannot clear it.
- `_gateway_ready` is a **precondition**, not a phase: the rollout's Phase B
  makes every agent-runner depend on the gateway (each runner's preflight
  refuses to stop services it cannot then restart), and the local leg's start
  is deliberately un-gated, so `rc == 0` there does not mean the gateway is
  serving. The gate probes the same URL + endpoint + headers the runners will
  (`_repo`'s `probe_gateway_once`) so the two cannot disagree, exits early on
  the failures a wait cannot fix, and turns a bound expiry into
  `RolloutOutcome.INCOMPLETE`. It is the stronger of the two checks — off-box
  and authenticated, where the start's gate is loopback and roster-wide.
- `_gateway_ready` only answers for the **gateway**, so the local leg's other
  services need their own channel: its child `ava start` writes the refused
  session names to `$AVA_HOME/last_launch_failures`, and the orchestration reads
  it right after the leg returns (`shared/launch_failures.py`). A non-empty list
  downgrades the rollout to `RolloutOutcome.INCOMPLETE` with the sessions named
  in the aftermath block, and does not abort it — the gateway is serving and
  the agent-runners still need Phase B. A record rather than an exit code because
  the names cannot ride an integer and the parent's own roster is the pre-pull
  tree's. Prod's 2026-08-06 rollout is what its absence cost:
  `✗ failed to start ava-frontend`, `rc=0`, three dark minutes.
- **The orchestrating host is not one of its own Phase-B targets**
  (`_update_orchestration:_phase_b_targets`). A single box carries
  `agent-runner`, so it is in the rollout list and stays there for Phase 0's
  fetch and Phase A's pause — both idempotent with the local leg. Phase B is
  where that stops holding: the local leg has already checked this host out,
  migrated it and restarted it, so the `cluster_update` op is redundant, and
  its first act is killing the gateway the readiness gate has just blessed.
  That is what stranded two runners for a settle window on 2026-08-01 (issue
  #1151). Behind it, `_repo:_probe_gateway_or_die` gives a runner's preflight a
  bounded budget (`GATEWAY_PREFLIGHT_BUDGET_S`) before one refused dial becomes
  a decline — defense in depth for holes a rollout did not open, not a second
  half of the same fix. A lone single box therefore runs an empty Phase B and
  reports CLEAN, but is still put through the readiness gate: nothing else asks
  whether its gateway came back.

## Key dependencies

- [[cli/commands/commands.ava.okf.md]] — command-module ownership and dispatch
- [[start-readiness.ava.okf.md]] — the ordinary `ava start` readiness contract
