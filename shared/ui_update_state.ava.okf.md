---
type: doc
title: Cluster UI Update State
description: Generation-guarded persistent ownership of Gate's updating page during a whole-cluster rollout or restart.
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
`started_at`, and diagnostic `updated_at`/phase/origin; normal completion
removes it.

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
- `$AVA_HOME/deploy-state.owner.lock` serializes owner publication against
  recovery proof and its destructive action. Normal publication holds it only
  briefly. `$AVA_HOME/deploy-state.lifecycle.lock` serializes long local
  start/stop/pause sections and remains the hold-watchdog's 0.1-second probe.
  Recovery takes the resource lock before the owner lock. `spawn_update`
  publishes its durable pending handoff and session record under both, then
  releases the owner lock before draining and spawning. The handoff prevents
  destructive recovery through that gap. The updater child stops only after
  the parent releases the long resource section. New rollout/restart triggers
  refuse a pending or uncleared updater handoff before their session check can
  mistake the pause-to-spawn gap for an idle host.
- Each mutex has an atomically replaced `.holder.json` sidecar with the last
  holder's PID, purpose, start time and held/released state. It is diagnostic;
  the OS lock remains the authority. A bounded wait names the last holder.
- A hard-killed owner leaves the marker as honest interrupted-update state.
  `ava cluster recover`, or stranded-pause automatic recovery after the same
  no-live-owner proof, may unpause only when the exact updater handoff and
  bootstrap/normal recovery envelope is terminal-clearable; successful unpause
  then force-clears this UI marker.

A rollback to pre-v2 code is outside the supported contract: rollback targets
are recent known-good releases, and a pre-v2 `{posture, updated_at}` file now
projects invalid (fail-safe) rather than being adopted — no host runs a pre-v2
writer (fleet verified 2026-09-20).

## Projection semantics

- Missing marker: inactive.
- Valid v2 marker: updating until its exact owner or recovery clears it. Age
  never changes the classification or invents a progress diagnosis.
- Pre-v2 `{posture, updated_at}` shapes: retired — they project invalid (no
  pre-v2 writer remains; fleet verified 2026-09-20).
- Malformed/unknown marker: invalid → Gate renders Service unavailable and
  emits a rate-limited warning; it never guesses that an update exists.

Gate is an ordinary root-owned service. A full root transition may stop its
entry listener; the persisted marker does not promise uninterrupted serving.
While running, Gate reads one immutable snapshot per HTTP request. An active snapshot
always renders System updating; without one, a gateway/app transport failure
renders Service unavailable. The two failure phases cannot invent different
states.

An already-open SPA never owns this page or its clock. `cluster_update_started`
and a lightweight same-origin `GET /__ava/deploy-state` poll share one reload
latch and only ask Gate to re-project the current URL. The endpoint returns
`{status,generation}` with `no-store` before any gateway/app probe.

Stable v2 generation/`started_at` ownership is guaranteed by the lock-winning
child that runs this code, from the introducing rollout onward; every host runs
a v2 writer (fleet verified 2026-09-20).
