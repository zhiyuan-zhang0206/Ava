---
type: doc
title: Home Lifecycle Mutex
description: The home-local advisory lock that serializes long start/stop/pause transitions with a bounded wait.
tags:
- deploy
- state
---

# Home Lifecycle Mutex

## What it is

`base/deploy/lifecycle/home_lifecycle_locks.py` owns one OS advisory lock under `$AVA_HOME`:

- `resource_lock` (`deploy-state.lifecycle.lock`) serializes long local
  start/stop/pause transitions with a bounded wait.

## Diagnostics

The mutex has an atomically replaced `.holder.json` sidecar with the last
holder's PID, purpose, start time and held/released state. It is diagnostic;
the OS lock remains the authority. A bounded wait that times out names the last
holder in its error.

## Invariants

- The lock file name is stable. Renaming it would split mutual exclusion
  between processes built from different revisions.
- The module keeps no UI state. Gate keeps none either: a stop stops Gate
  with the rest of root, and a `$AVA_HOME/deploy-state.json` left by
  the retired updater is inert.
