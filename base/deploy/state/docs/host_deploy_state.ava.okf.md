---
type: doc
title: Host Deploy State
description: One row per machine in `host_deploy_state` — its idle/paused posture after native service drain.
tags:
- deploy
- liveness
---

# Host Deploy State

## What it is

`base/deploy/state/host_deploy_state.py` is the read/write API for the `host_deploy_state` table — one row per machine in the central DB answering the host-level question the old signals (the `cluster_paused` file, `updating.flag`, session probing, updater-log mtime) answered with files and process-local guesses. Those file signals were retired by the old-signal sweep; this row is the contract.

- **posture** (`idle` / `paused`) — `paused` marks service shutdown after native drain; the admission journal keeps in-flight APIs available during the drain. A missing row reads as `idle` — every consumer's default. A transition writes `posture` and `updated_at` and nothing else.
- **`db_now`** — every `HostDeployState` carries the database's own clock, selected in the same statement as the row, so an age judgment never subtracts across two machines' clocks.

The table carries only `machine`, `posture` and `updated_at`: the updater lease, the pause-window anchor and the stranded-hold record were dropped by `20261001T055030_drop-retired-deploy-and-watcher-storage`.

## Core Responsibilities

### Posture transitions (writers)

- `ops/cluster/pause.py` — service shutdown sets paused posture and `unpause_local_cluster` restores idle posture; under a held journal it refuses failed receipts, and stopped services until `ava start` passes readiness. `is_paused()` reads the row (a read failure reads as NOT paused — the conservative direction). The gateway 503 middleware and the `status`/`cluster` endpoints go through it.
- `cli/commands/lifecycle/start.py` tail — `set_posture('idle')` after a successful `ava start`.

### Observers (readers)

- `ops/deploy_window.py:_posture_signal` — the deploy-window signal (any machine mid-deploy) reads `read_all()` instead of probing each host's ops server: the old probe died with the daemon it observed mid self-update; the row is written outside the restarted services and survives the window. A non-idle row keeps the signal active until that host's `ava start` returns it to `idle` — the conservative direction, except on an operator-excluded machine, whose row is ignored and logged: nothing returns that posture to `idle` while the exclusion lasts (issue #2160).

## Key Dependencies

- [[base.ava.okf.md|Base Library]] — layering: `base` must not import `cli`/`gateway`; identity from `base.cluster.machine`, DB from `base.db`
- [[base/deploy/state/docs/state.ava.okf.md|Deployment state]] — current host posture and code-version responsibilities; the [deployment lease was retired](../../../../docs/decisions/runtime/updates/release/2026-09-30-remove-deployment-lease.md)

## Entry Points

- `base/deploy/state/host_deploy_state.py:read` / `read_all` — this machine's row / every machine's rows
- `set_posture` — the posture transition

## Notes

- `deployment_state` is a singleton row whose live consumer is the [[base/db/docs/code-version-gate.ava.okf.md|code-version gate]] (`min_code_version`); nothing takes a cluster deploy lease on it any more. Agents carry their own leases in `agents_meta`.
- The home lifecycle mutex that serializes local start/stop is in [[home_lifecycle_locks.ava.okf.md|Home Lifecycle Mutex]].

The controller-driven stranded-hold writer, budget, local note queue, heartbeat
alert, and status projection have been removed; their five physical columns
were dropped by `20261001T055030_drop-retired-deploy-and-watcher-storage`.
