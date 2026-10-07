---
type: doc
title: "R1 — State & Liveness (explicit model)"
description: "Planned concept model (v3.3, awaiting user): deployment state becomes two explicit tables, liveness becomes leases, the event stream returns to pure facts, migrations get a single applier. Final state of a Big Bang migration. (The watcher-registry piece of this plan shipped, then was reversed 2026-09-27 — docs/decisions/runtime/updates/recovery/2026-09-27-watchers-are-never-restarted.md; watchers are not part of this frame any more.)"
tags:
- design
- planned
- state
- liveness
---

# R1 — State & Liveness (explicit model)

> Design lead #2861 · design concept v3.3 (2026-08-07) · **design-phase node — the current system is NOT this; see the as-is nodes linked at the bottom.**

> **As landed:** the cluster deploy lease and the per-host updater lease described below were retired with the in-place updater ([decision](../../../docs/decisions/runtime/updates/release/2026-09-30-remove-deployment-lease.md)). `host_deploy_state` is `machine`, the `idle`/`paused` posture and `updated_at`, and `deployment_state` is `id` plus the code-version gate's `min_code_version` ([decision](../../../docs/decisions/runtime/updates/release/2026-10-01-contract-the-retired-deploy-storage.md)). The `stable` / `updating` / `settling` phases, the settle note and `recover` do not exist; agent leases (`agents_meta.lease_expires_at`) landed as designed, and the agent state machine has three states (`restarting` was retired).

## Problem in one sentence

"Now what is happening?" has no authority today: deployment status is the implicit conjunction of 6 signals (DB lock row, two flag files, session names, updater log mtime, orchestrator-local variables); agent liveness is a self-report chain that breaks; `agents_meta.status` transitions live in 8+ scattered SQL statements; the event stream doubles as a state register. Every consumer writes its own predicate subset — three heal controllers diverged into two settle semantics in one 48h window (#1020/#1074/#1116).

## The two ideas (the whole model)

1. **Single source of truth** (see [[okf/design/design.ava.okf.md|lexicon]]): every "what is the state now" question has one authority storage, one read API, one set of legal transitions. Signals may be scattered (evidence); the interpretation is one (the court).
2. **Liveness = lease** (see [[okf/design/design.ava.okf.md|lexicon]]): every "is X alive" is a lease-expiry judgment. Voluntary self-report only accelerates convergence, never required.

## Concept model

### Deployment state: two tables + a cluster UI marker

**`deployment_state`** (cluster-level, one row) — the existing `cluster_update_lock` becomes a full state row: `phase`, `kind` (`rollout`/`restart`/`update` — replaces session-name probing), `lease_holder`/`lease_expires_at` (existing TTL+renew kept), `settle_hosts`/`settle_note`, `last_outcome` (a record, not a state).

**`host_deploy_state`** (host-level, one row per host) — replaces the `cluster_paused` file, `updating.flag`, session probing, updater-log-mtime liveness: `posture` (`idle`/`paused`/`converging`), `updater_lease_expires_at`, `updated_at`.

**Cluster UI marker** (`$AVA_HOME/deploy-state.json`) — retired with the in-place updater that wrote it. No lifecycle produces it and Gate does not read it: a stop takes Gate down with the rest of root. A leftover file is inert.

### Phases: three states, not five

`stable` (no lease, no orchestration) → `updating` (lease held) → `settling` (lease + settle note, waiting for named hosts). `recover` is a controlled early transition (settle→stable, an operator action, not a state); `stalled` is a judgment derived from updater-lease expiry, not a state. Failure is recorded in `last_outcome` — a fact, not a phase.

### Liveness: registry × lease, one mechanism, many objects

The registry×lease frame for every managed object and the single `alive` predicate: [[okf/design/r1-state-liveness/liveness.ava.okf.md]] (watchers were pulled back out of this frame 2026-09-27 — docs/decisions/runtime/updates/recovery/2026-09-27-watchers-are-never-restarted.md).

### Agent state machine: one matrix

Four states (`running`/`idling`/`restarting`/`terminated` as designed; `restarting` was retired, so three today, matching [[base/docs/agents-contract.ava.okf.md]]) with one transition matrix: each transition is one row (from-set → to → allowed writer → side effects); all writers go through the single entry `agent_state.transition()`. Batch-adjudication edge conditions move from comments into the state graph + tests.

### Migration application authority

Schema version is deployment state: applied set vs code-required set is the drift judgment (schema-ahead guard kept as last line of defense). **The orchestrator (rollout only) is the sole applier** — applying a migration outside the framework is an illegal transition (2026-08-07 incident). Discovery recognizes only the tracked registry; untracked files are ignored + warned on convergence. Advancement (rollout) and repair (schema controller) each get one owner, both reading the same sets.

### Event stream back to pure facts

Inspector statistics use cumulative or time-based windows over persisted observations. Watchdog dedup uses one truth table (`delivery_watchdog_alerted`). State markers never enter the event stream.

## The five invariants

1. **State vs liveness separated**: status = lifecycle intent; lease = process-level fact; `running + lease expired = zombie`.
2. **Single read API**: `deployment_state()` / `agent_liveness()` are the only interpreters; consumers never compose signals.
3. **Single writer + single state-machine implementation**: one writer per state field, all through the state-machine module; logic exists only in Python (shell/cmd translate parameters). Instance: migrations are applied only by the orchestrator.
4. **Liveness = lease**: any "is X alive" is a lease-expiry judgment; critical paths never depend on self-report (#961 regression-locked).
5. **Event stream appends facts only**: state markers do not enter the event table.

## Open decision points

- **Q1 — state storage shape**: two tables (recommended — every state queryable/constrainable/testable) vs single table + JSONB hosts column (one "state object", host substate buried in JSONB).
- **Q2 — phase enumeration**: three states (recommended — failure is a fact in `last_outcome`, not a phase) vs five (adds `recovering`/`failed`).

## Related as-is nodes

[[../../../cli/docs/cli.ava.okf.md]] · [[../../../agent/docs/agent.ava.okf.md]] · [[../../../gateway/docs/gateway.ava.okf.md]] · [[../../../base/docs/base.ava.okf.md]]
