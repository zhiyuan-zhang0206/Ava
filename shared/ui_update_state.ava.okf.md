---
type: doc
title: Cluster UI Update State
description: Generation-guarded persistent ownership of the always-up gate's updating page during a whole-cluster rollout or restart.
tags:
- deploy
- gate
- state
---

# Cluster UI Update State

## What it is

`shared/ui_update_state.py` owns `$AVA_HOME/deploy-state.json`, the durable
fact that a whole-cluster rollout/restart currently owns the fleet UI. While
active the file contains one schema-v2 generation, kind, stable RFC3339
`started_at`, diagnostic `updated_at`/phase/origin, and legacy
`posture="paused"`; normal completion removes it.

This is deliberately separate from host posture. `host_deploy_state` remains
the online control-plane authority for pause/converge/updater liveness, while
the UI marker spans gateway/frontend process replacement and the full Phase-B
tail. `ava start`, pause/unpause, and updater lease renewal never write or clear
the UI marker.

## Concurrency contract

- Every begin/phase/clear holds `$AVA_HOME/deploy-state.lock` through
  `shared.platform.file_lock`.
- Writes are same-directory temp + file fsync + atomic replace + directory
  fsync, so the lock-free gate reader sees only a complete old/new snapshot.
- Phase updates and completion are generation-CAS operations inside the lock.
  A late process from generation A cannot overwrite or unlink generation B.
- A hard-killed owner leaves the marker as honest interrupted-update state.
  `ava cluster recover`, or stranded-pause automatic recovery after the same
  no-live-owner proof, may unpause only when the exact updater handoff and
  bootstrap/normal recovery envelope is terminal-clearable; successful unpause
  then force-clears this UI marker.

The marker does not cover `cluster rollback` yet. A rollback deliberately boots
an older target whose host-posture writer can overwrite this shared compatibility
path with legacy v1 `idle`; a future cross-version-safe rollback protocol needs
a separate file/restamp contract. This PR must not claim that unsafe ownership.

## Projection semantics

- Missing marker: inactive.
- Valid v2 marker: updating until its exact owner or recovery clears it. Age
  never changes the classification or invents a progress diagnosis.
- Legacy `{posture, updated_at}`: paused/converging is updating with
  `updated_at` as the one-rollout compatibility start; idle is inactive.
- Malformed/unknown marker: invalid → Gate renders Service unavailable and
  emits a rate-limited warning; it never guesses that an update exists.

The gate reads one immutable snapshot per HTTP request. An active snapshot
always renders System updating; without one, a gateway/app transport failure
renders Service unavailable. The two failure phases cannot invent different
states.

An already-open SPA never owns this page or its clock. `cluster_update_started`
and a lightweight same-origin `GET /__ava/deploy-state` poll share one reload
latch and only ask Gate to re-project the current URL. The endpoint returns
`{status,generation}` with `no-store` before any gateway/app probe.

The rollout that introduces this code is necessarily started by the old
in-memory orchestrator and v1 posture writer. The new Gate can parse that v1
marker, but stable v2 generation/`started_at` ownership is guaranteed only for
a later rollout or restart whose lock-winning child runs this code. A rollback
to older code likewise falls back to the legacy contract.
