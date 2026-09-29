---
type: doc
title: Cluster Deploy Lease
description: Retained deployment_state SQL exclusion and exact pending-publication guards.
tags:
- deploy
- liveness
---

# Cluster Deploy Lease

`shared/cluster_lock.py` owns the retained `deployment_state` singleton lease.
Runtime admission, maintenance, stop and recovery still consult its exclusion
and pending-publication evidence. Removing the retired updater command graph
does not remove these SQL guards.

## Lease and publication contracts

- `acquire_update_lock` atomically claims a free or expired lease. Durable
  `managed_writer_evidence.pending` refuses a generic takeover even after expiry.
- `renew_update_lock` and `release_update_lock` require the exact holder. Release,
  settle conversion and settle release cannot discard pending publication evidence.
- `read_update_lease` returns holder, age, expiry, kind and structured settle facts.
  `update_lock_holder` exposes the same authority to admission and diagnostics;
  a refused acquire logs which guard refused it.
- Settle holds remain distinct from executing leases. Their host set and bounded
  TTL let `ops.deploy_window` clear a converged settle hold without releasing an
  executing operation's lease.

## Dependencies and cutover

`shared/deploy_timing.py` supplies the retained no-progress bound;
`shared/host_deploy_state.py` supplies host posture and lease evidence.
`shared/runtime_admission.py` consumes the SQL admission guards.

The retired updater result publisher and status projections are absent.
`cluster_last_update` and unused outcome columns in `deployment_state` remain
physical schema pending an explicit database cutover; they have no surviving
last-update producer or compatibility projection. The prepared release operation
journal owns new operation status. Replacing SQL admission authority and dropping
retired physical columns are separate work.
