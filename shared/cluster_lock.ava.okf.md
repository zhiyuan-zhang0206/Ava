---
type: doc
title: Cluster Deploy Lease
description: The `deployment_state` singleton row — the cluster's "a deploy owns this cluster" lease with phase/kind/settle/last_outcome — the single authority every healer consults before acting mid-deploy.
tags:
- deploy
- liveness
- r1
---

# Cluster Deploy Lease

## What it is

`shared/cluster_lock.py` is the read/write API for the **`deployment_state` singleton row** (id = 1, central DB) — the cluster-level half of the R1 deployment-state model. One row answers the whole cluster's "is a deploy happening, of what kind, and what was the last result" — replacing the old `cluster_update_lock` table (retired), session-name probing, and part of `cluster_last_update` (whose columns are mirrored here as `last_outcome`).

This row is the cluster's single **"intentionally mid-transition" signal, not just a mutex**. Every automated healer reads it before acting — the pin / code / schema controllers, the stranded-pause controller, `ava status`, and `ava stop`'s unpause discriminator all ask "is a deploy running?" of this row and defer when one is. There is deliberately no second "deploy in progress" flag: a second source of truth for the same fact is free to disagree with this one, which is the whole bug class this row exists to close (the 2026-06-01 schema collision; the 2026-07-29 second-rollout collision).

## Core Responsibilities

### The lease (mutual exclusion with crash-reclaim)

- `acquire_update_lock(holder, *, kind, ttl_s)` — the atomic compare-and-set (CAS) against a free row or an expired holder; on success writes `phase='updating'` + `kind`. Returns False when a *live* holder still holds it or `managed_writer_evidence.pending` requires its own exact publication recovery. Generic recovery has the same pending guard: publication state outlives lease expiry and cannot be stranded by replacing only its holder.
- `renew_update_lock` / `release_update_lock` — renewal is driven from the Phase-B poll, so `LOCK_TTL_S` (1800 s) bounds only how long a *crashed* holder blocks the next rollout; release returns the row to `phase='stable'` and clears `kind` + settle fields only after checked publication handling has cleared durable `pending`. Settle conversion and settle release carry the same atomic guard, so no generic lease transition can strand retained publication evidence.
- `read_update_lease` → `DeployLease` (holder / held_for_s / expires_in_s / note / kind) — the row itself, for consumers that must explain the hold or tell an executing rollout apart from a settle hold (`DeployLease.awaits`); `update_lock_holder()` is the bare "is it held".

### Phase & kind (the explicit model)

- `phase` ∈ {`stable`, `updating`, `settling`} — three states, not five: `recover` is a controlled early transition (settle → stable, an operator action), `stalled` is a judgment derived from updater-lease expiry, not a state, and failure is a fact in `last_outcome`.
- `kind` ∈ {`rollout`, `restart`, `update`} — the explicit "what is happening", written at acquire (`_run_gateway_orchestration` passes `restart` for a restart-only bounce, `rollout` otherwise); `NULL` for a kind-less holder (a rollback). The runner-side `update` kind lives in `host_deploy_state`, not this row.
- **Settle holds** — `settle_update_lock(holder, hosts)`: an orchestration that ends with hosts still converging keeps the lease held on its own TTL (`SETTLE_TTL_S` = `NO_PROGRESS_TIMEOUT_S`) instead of releasing, so a second deploy cannot start into a half-transitioned cluster. The settle fact lands three ways in one statement: the structured `settle_hosts` array (the truth), `settle_note` (human-readable), and the legacy `note` column (kept for old readers and to scope `release_settle_hold`, which is `note IS NOT NULL`-guarded so it can only ever clear a settle hold).

### last_outcome (a record, not a state)

`shared/last_update.py` dual-writes `deployment_state.last_outcome` (outcome / failing_step / started_at / ended_at / origin / target_sha / observed_by / log_path / pin_advanced) in the same transaction as its own table — same writer, same transaction. The `holder` column is deliberately NOT mirrored: `deployment_state.holder` is the deploy lease's, owned by this module. `outcome` stays NULL while a rollout executes (and if it dies) — the reader derives RUNNING/ORPHANED from the lease, exactly as before.

## Key Dependencies

- [[host_deploy_state.ava.okf.md|Host Deploy State]] — the host-level half of the model
- [[okf/design/r1-state-liveness/r1-state-liveness.ava.okf.md|R1 state & liveness design]] — the deployment-state model
- `shared/deploy_timing.py` — `NO_PROGRESS_TIMEOUT_S`, the family's one no-progress definition (settle TTL, Phase-B poll, updater stall)
- `shared/last_update.py` — the `last_outcome` dual-writer
- `cli.ava.okf.md` — `ava cluster update` orchestration (the lease's main holder)

## Entry Points

- `acquire_update_lock` / `renew_update_lock` / `release_update_lock` / `settle_update_lock` / `release_settle_hold` / `read_update_lease` / `update_lock_holder` — `shared/cluster_lock.py`
- `shared/last_update.py:begin_update` / `finish_update` / `note_observed_recovery` — the `last_outcome` writers
- `cli/commands/update.py:_run_gateway_orchestration` — the primary holder (kind = restart|rollout)

## Notes

- A connection-scoped Postgres advisory lock would auto-release on disconnect but cannot model a lease that must outlive the many short connections a multi-phase, multi-process rollout opens — the TTL'd row can.
- `deployment_state` was created by the R1 expand migration and backfilled from `cluster_update_lock` (phase derived from the old row's own semantics: settle note → settling, held lease → updating, free → stable). The old `cluster_update_lock` table is retired; `cluster_last_update` is still dual-written and read by the `last_update` readers until they switch to `last_outcome` — this row is the contract.
