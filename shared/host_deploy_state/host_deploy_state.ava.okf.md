---
type: doc
title: Host Deploy State
description: One row per machine in `host_deploy_state` — posture (idle/paused/converging), the pause-window anchor and the retained updater liveness lease — the host-level half of the R1 deployment-state model.
tags:
- deploy
- liveness
- r1
---

# Host Deploy State

## What it is

`shared/host_deploy_state.py` is the read/write API for the `host_deploy_state` table — one row per machine in the central DB answering the two host-level questions the old signals (the `cluster_paused` file, `updating.flag`, session probing, updater-log mtime) answered with files and process-local guesses. Those file signals were retired by the old-signal sweep; this row is the contract.

- **posture** (`idle` / `paused` / `converging`) — `paused` marks service shutdown after native drain; the admission journal keeps in-flight APIs available during the drain; `converging` is the updater actually running on this host (its lease is live). A missing row reads as `idle` — every consumer's default.
- **paused_at** anchors the current pause window. It is set entering `paused`,
  preserved through `converging`, and cleared on `idle`.
- **updater lease** is retained SQL observation for admission and deploy-window
  readers. The retired updater commands no longer renew it. A lease or timestamp
  does not grant native process ownership or authorize a replacement updater.

## Core Responsibilities

### Posture transitions (writers)

- `ops/cluster_pause.py` — `pause_local_cluster` drains through the admission journal without closing APIs; service shutdown sets paused posture and `unpause_local_cluster` restores idle posture. `is_paused()` reads the row (a read failure reads as NOT paused — the conservative direction). `local_resume_refusal()` exposes the resume guard's verdict read-only (services stopped under a held journal → `ava start` must pass readiness first), so a caller deciding whether to attempt a compensating unpause — the rollout finalize tail — states the reason once instead of walking a doomed ladder (issue #2162). The gateway 503 middleware and the `status`/`cluster` endpoints go through it.
- `cli/commands/start.py` tail — `set_posture('idle')` after a successful `ava start`.

### Retained lease reader

Nothing arms, renews or clears `updater_lease_expires_at` any more: the retired
updater's lease writers and its host mutex (`$AVA_HOME/run/updater.lock`) are
gone. `updater_lease_live()` / `HostDeployState.updater_live` still read the
column, so a lease an upgraded host carried over keeps recovery and the
deploy-window signal conservative until it expires. The column itself stays
until the explicit schema cutover.

### Observers (readers)

- `ops/deploy_window.py:_remote_orchestration` — deploy-window signal 2 (another machine mid-deploy) reads `read_all()` instead of probing each host's ops server: the old probe died with the daemon it observed mid self-update; the row is written outside the restarted services and survives the window. A stale `converging` row keeps the signal active — the conservative direction, except on an operator-excluded machine, where it counts only with a live updater lease behind it: nothing recovers that posture while the exclusion lasts (issue #2160).

## Key Dependencies

- [[shared.ava.okf.md|Shared Libraries]] — layering: `shared` must not import `cli`/`gateway`; identity from `shared.machine`, DB from `shared.db`
- [[okf/design/r1-state-liveness/r1-state-liveness.ava.okf.md|R1 state & liveness design]] — the two-table deployment-state model

## Entry Points

- `shared/host_deploy_state.py:read` / `read_all` — this machine's row / every machine's rows
- `set_posture` — the posture transition
- `updater_lease_live` — the retained lease reader

## Notes

- The cluster-level counterpart is [[cluster_lock.ava.okf.md|the cluster deploy lease]] (`deployment_state`); agents carry their own leases in `agents_meta`.
- The home lifecycle mutexes that serialize local start/stop/pause with recovery are in [[home_lifecycle_locks.ava.okf.md|Home Lifecycle Mutexes]].

The controller-driven stranded-hold writer, budget, local note queue, heartbeat
alert, and status projection have been removed. Their five physical columns
remain unused until the explicit cleanup described in
[the lifecycle plan](../../future/infra/unified-cluster-lifecycle.md).
