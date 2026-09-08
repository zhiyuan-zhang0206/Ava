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
  rollout parent's boundary: it retains native admission holds and leaves posture
  `converging`. A pure agent-runner Phase-B child completes readiness and its
  host-local transition to `idle`, so the gateway's
  Phase-B poll can observe it as converged.
- A rollout a stuck host is dragging has a formal cancel now (`ava cluster
  cancel`, P1 2026-08-30): `ops.ops_cluster.cluster_cancel_op` SIGINTs the
  orchestration's own pid — read from the deploy lease's holder string — never
  the session, so the orchestration's `finally` runs its recovery (compensating
  resume, lease release or settle hold over the unconverged, maintenance marker
  cleared). It refuses on every unprovable reading; a settle hold and a dead
  holder stay `ava cluster recover`'s job.
- Failed-update schema recovery is the narrow exception to runtime-owner DB
  authentication: it targets this cluster's database through the gateway's
  local Postgres trust socket, so a partially applied credential transition
  cannot prevent last-known-good rollback. Manual `ava cluster rollback` keeps
  using the runtime owner connection. A dedicated pre-mutation connection error
  proves unchanged schema; any unexpected error after rollback begins leaves
  code on the new revision and reports schema state as unknown.

- Deterministic prepare failures block; duration estimates are telemetry only.

## Readiness and Phase B

- Phase 0 freezes the eligible runner set before pause. Every participant
  must fetch the target and acknowledge native drain in Phase A before the
  gateway advances to migration. An unreachable or failed participant aborts
  this rollout; it is not removed from the barrier while still serving.
- [[cli/commands/rollout-boundary/phase-b.ava.okf.md]] specifies the runner
  convergence evidence, stall judgments, retry ownership and deadlines.
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
- Normal local stop/update uses the shared native pause: ordinary restart,
  checkpoint flush and original work exit before services are signalled.
  Stop failures abort the next update stage. Default timeout is failure,
  while explicit force remains a separate policy. Persistent agent and schedule
  PTYs remain open across update; PostgreSQL/Redis remain up for migration.
  First deployment needs a verified bootstrap: disk code does not fence old daemons.

## Key dependencies

- [[cli/commands/commands.ava.okf.md]] — command-module ownership and dispatch
- [[start-readiness.ava.okf.md]] — the ordinary `ava start` readiness contract
