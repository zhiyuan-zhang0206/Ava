---
type: doc
title: Home Lifecycle Mutexes
description: The two home-local advisory locks that serialize start/stop/pause with pause-owner publication and cluster recovery.
tags:
- deploy
- state
---

# Home Lifecycle Mutexes

## What it is

`shared/home_lifecycle_locks.py` owns two OS advisory locks under `$AVA_HOME`:

- `resource_lock` (`deploy-state.lifecycle.lock`) serializes long local
  start/stop/pause transitions with a bounded wait.
- `lifecycle_lock` (`deploy-state.owner.lock`) serializes the short
  pause-owner publication against recovery's liveness proof and destructive
  action.

Recovery (`ava cluster recover`) takes the resource lock before the owner lock,
the same order as the stop and resume operations in `ops/ops_cluster.py`.

## Diagnostics

Each mutex has an atomically replaced `.holder.json` sidecar with the last
holder's PID, purpose, start time and held/released state. It is diagnostic;
the OS lock remains the authority. A bounded wait that times out names the last
holder in its error.

## Invariants

- The lock file names are stable. Renaming them would split mutual exclusion
  between processes built from different revisions.
- The module keeps no UI state. Gate keeps none either: a release transition
  stops Gate with the rest of root, and a `$AVA_HOME/deploy-state.json` left by
  the retired updater is inert.
