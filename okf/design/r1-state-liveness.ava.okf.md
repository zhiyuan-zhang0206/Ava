---
type: doc
title: "R1 — State & Liveness (explicit model)"
description: "Planned concept model (v3.3, awaiting user): deployment state becomes two explicit tables, liveness becomes leases, watchers get a registry, the event stream returns to pure facts, migrations get a single applier. Final state of a Big Bang migration."
tags:
- design
- planned
- state
- liveness
---

# R1 — State & Liveness (explicit model)

> Design lead #2861 · design concept v3.3 (2026-08-07) · **design-phase node — the current system is NOT this; see the as-is nodes linked at the bottom.**

## Problem in one sentence

"Now what is happening?" has no authority today: deployment status is the implicit conjunction of 6 signals (DB lock row, two flag files, session names, updater log mtime, orchestrator-local variables); agent liveness is a self-report chain that breaks; `agents_meta.status` transitions live in 8+ scattered SQL statements; the event stream doubles as a state register. Every consumer writes its own predicate subset — three heal controllers diverged into two settle semantics in one 48h window (#1020/#1074/#1116).

## The two ideas (the whole model)

1. **Single source of truth** (see [[okf/design/design.ava.okf.md|lexicon]]): every "what is the state now" question has one authority storage, one read API, one set of legal transitions. Signals may be scattered (evidence); the interpretation is one (the court).
2. **Liveness = lease** (see [[okf/design/design.ava.okf.md|lexicon]]): every "is X alive" is a lease-expiry judgment. Voluntary self-report only accelerates convergence, never required.

## Concept model

### Deployment state: two tables + a mirror file

**`deployment_state`** (cluster-level, one row) — the existing `cluster_update_lock` becomes a full state row: `phase`, `kind` (`rollout`/`restart`/`update` — replaces session-name probing), `lease_holder`/`lease_expires_at` (existing TTL+renew kept), `settle_hosts`/`settle_note`, `last_outcome` (a record, not a state).

**`host_deploy_state`** (host-level, one row per host) — replaces the `cluster_paused` file, `updating.flag`, session probing, updater-log-mtime liveness: `posture` (`idle`/`paused`/`converging`), `updater_lease_expires_at`, `updated_at`.

**Mirror file** (`$AVA_HOME/deploy-state.json`) — a projection cache of the state table on the host's filesystem for the offline window (gateway/DB unreachable, "updating" page). Only host-level transitions write it, by the same module that writes the DB; cluster-level fields never land in it. Write failure warns, never blocks — a degraded label, not an authority.

### Phases: three states, not five

`stable` (no lease, no orchestration) → `updating` (lease held) → `settling` (lease + settle note, waiting for named hosts). `recover` is a controlled early transition (settle→stable, an operator action, not a state); `stalled` is a judgment derived from updater-lease expiry, not a state. Failure is recorded in `last_outcome` — a fact, not a phase.

### Liveness: registry × lease, one mechanism, many objects

Two questions frame every managed object: **registry** answers "should it exist?" (persistent row: who, what type, what schedule, current state); **lease** answers "is it alive?" (periodic renewal, expiry = dead).

| Object | Registry | Lease | Renewed by | Observers |
|---|---|---|---|---|
| Cluster orchestration | `deployment_state` row | `lease_expires_at` | orchestrator (existing) | controllers, status, recover |
| Agent process | `agents_meta` row (exists) | `agents_meta.lease_expires_at` (new column) | the agent itself (light timer — idle renews too) | quiesce, reaper, frontend, heartbeat guard |
| Updater process | `host_deploy_state` row | `host_deploy_state.updater_lease_expires_at` | the updater | stalled judgment, Phase B polling |
| **Watcher** | **`agent_watchers` table (new)** | `lease_expires_at` | the watcher process | agent reconcile (rebuild), reaper |

Today's gaps fall out of the frame: agents have a registry but no lease; watchers have a session but no registry (the #1014 class, 4th recurrence — a kill of the session left nothing knowing it should exist; sessions now survive rollouts, and the watcher registry closed the rest); updaters have neither.

**Watcher rebuild**: `ava.watcher.at/cron/launch` writes a registry row at creation; agent boot reconciles — row exists but session missing → rebuild (cron restores from its expression; missed one-shots mark `missed` + alert). Lease expiry → reaper collects.

**Single predicate**: `alive := status ∈ {starting, running, idling} ∧ lease unexpired` — defined once, imported everywhere. `running + lease expired = zombie` → reaper collects. `hibernating` (swapped out): no renewal, reaper-exempt.

### Agent state machine: one matrix

Seven states (`allocated`/`starting`/`running`/`idling`/`restarting`/`terminated`/`hibernating`, matching [[shared/agents-contract.ava.okf.md]]) with one transition matrix: each transition is one row (from-set → to → allowed writer → side effects); all writers go through the single entry `agent_state.transition()`. Batch-adjudication edge conditions move from comments into the state graph + tests.

### Migration application authority

Schema version is deployment state: applied set vs code-required set is the drift judgment (schema-ahead guard kept as last line of defense). **The orchestrator (rollout only) is the sole applier** — applying a migration outside the framework is an illegal transition (2026-08-07 incident). Discovery recognizes only the tracked registry; untracked files are ignored + warned on convergence. Advancement (rollout) and repair (schema controller) each get one owner, both reading the same sets.

### Event stream back to pure facts

`agents_meta.last_compact_at` (synchronous update) replaces the events-table `OFFSET 1` hack; watchdog dedup gets one truth table (`delivery_watchdog_alerted`), dropping the memory double-write. State markers never enter the event stream.

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

[[../../cli/cli.ava.okf.md]] · [[../../agent/agent.ava.okf.md]] · [[../../gateway/gateway.ava.okf.md]] · [[../../shared/shared.ava.okf.md]]
