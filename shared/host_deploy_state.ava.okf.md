---
type: doc
title: Host Deploy State
description: One row per machine in `host_deploy_state` — posture (idle/paused/converging), the pause-window anchor and the updater's liveness lease — the host-level half of the R1 deployment-state model, plus its updater mutex.
tags:
- deploy
- liveness
- r1
---

# Host Deploy State

## What it is

`shared/host_deploy_state.py` is the read/write API for the `host_deploy_state` table — one row per machine in the central DB answering the two host-level questions the old signals (the `cluster_paused` file, `updating.flag`, session probing, updater-log mtime) answered with files and process-local guesses. Those file signals were retired by the old-signal sweep; this row is the contract.

- **posture** (`idle` / `paused` / `converging`) — `paused` marks service shutdown after native drain; the admission journal keeps in-flight APIs available during the drain; `converging` is the updater actually running on this host (its lease is live). A missing row reads as `idle` — every consumer's default.
- **paused_at** — the moment the current pause window started: set entering `paused`, preserved through `converging`, cleared on `idle`. It anchors *both* readers that have to ask "does this evidence belong to the current update?" — the updater-outcome log reader (what the `cluster_paused` file mtime used to be) and `updater_expired` below; `updated_at` cannot serve because every transition inside the window bumps it.
- **updater lease** (`updater_lease_expires_at`) — the updater process's liveness as a lease-expiry judgment: `updater_live` = still working; `updater_expired` = the stalled judgment (stalled-updater controller, Phase-B poll). The two are not complements. Nothing clears the column on the way into a pause (`set_posture` owns posture alone, so a pause landing mid-rollout cannot erase a live claim), so an uncleared expiry outlives its own update and the next one inherits it — `updater_expired` therefore dates the arming (`expires - UPDATER_LEASE_TTL_S`) against `paused_at` and reads a lease armed before this window as no evidence at all. **Every timestamp in that arithmetic is Postgres'** — the expiry is written as `now() + make_interval(...)` and the reader brings back `now()` as `db_now` in the same statement — because the row is written by the runner and judged on the gateway, and nothing bounds two machines' clock drift (a Windows host resuming from sleep before NTP converges reaches minutes). See [the decision record](../decisions/2026-08-12-a-written-ending-outranks-the-updater-lease.md).

## Core Responsibilities

### Posture transitions (writers)

- `ops/cluster_pause.py` — `pause_local_cluster` drains through the admission journal without closing APIs; service shutdown sets paused posture and `unpause_local_cluster` restores idle posture. `is_paused()` reads the row (a read failure reads as NOT paused — the conservative direction). The gateway 503 middleware and the `status`/`cluster` endpoints go through it.
- `cli/commands/start.py` tail — `set_posture('idle')` after a successful `ava start`; `spawn_update`'s failed-chain rollback does the same.
- `ops/controllers/stranded_pause.py` — `paused` AND `converging` gate service resurrection while an updater may be running. The native maintenance journal also blocks watchdog admission and cannot be cleared by stranded-pause recovery.

### Updater lease (writers)

- `cli/commands/_updater_lease.py` — the Windows native-chain seam. Its host-local handoff claim is a hard pre-mutation gate; after that claim, the Postgres updater-lease touch/clear remains fail-soft liveness observation.
- `cli/commands/_update_agent_runner.py` — the POSIX/in-process self-update first claims the host-local handoff, then wraps its body with fail-soft Postgres touch / finally clear.
- `touch_updater_lease` enters `converging` and arms the lease (`UPDATER_LEASE_TTL_S` = `NO_PROGRESS_TIMEOUT_S`, 900 s — the family's one no-progress definition); `clear_updater_lease` drops the lease and **leaves the posture alone** — `unpause` / `ava start` owns the return to `idle`, and a chain-tail clear must not stamp a just-converged host back to `converging`. A crashed updater leaves the lease to expire (and the posture at `converging`, which gates resurrection until the controller reaps it).

### Observers (readers)

- `cli/commands/_update_phase_b.py:_probe_verdict` — the stall verdict is the row, not the probe's wire fields: `idle` (or no row) → OK; live lease → still working; `paused` + no lease → transition window / legacy chain (never a stall); `paused` + expired lease or `converging` + no live lease → STALLED (2 confirmations). A row read failure is "cannot tell" (fail-soft).
- `ops/deploy_window.py:_remote_orchestration` — deploy-window signal 2 (another machine mid-deploy) reads `read_all()` instead of probing each host's ops server: the old probe died with the daemon it observed mid self-update; the row is written outside the restarted services and survives the window. A stale `converging` row keeps the signal active — the conservative direction.
- `ops/cluster_deploy.py:_updater_hung` — the stalled-updater reaper's liveness judgment: expired lease → hung; live lease → fine; no lease → legacy updater, fall back to log-mtime.
### Updater mutex

- **Updater mutex** (`$AVA_HOME/run/updater.lock`, flock/msvcrt): the updater lease is a LIVENESS claim, not a mutex — two updaters on one host can both hold live leases (the 2026-08-11 WinError 87/32 collision, task #1181). The lock is the mutual-exclusion half, held for the updater's whole run; the OS releases it when the holder dies, so there is no stale-lock handling. POSIX marks its fd inheritable so the post-checkout exec retains the flock; Windows keeps the parent alive while a child runs that continuation. Fail-soft: only a genuine concurrent holder returns False.

## Key Dependencies

- [[shared.ava.okf.md|Shared Libraries]] — layering: `shared` must not import `cli`/`gateway`; identity from `shared.machine`, DB from `shared.db`
- [[okf/design/r1-state-liveness/r1-state-liveness.ava.okf.md|R1 state & liveness design]] — the two-table deployment-state model
- `shared/deploy_timing.py` — `NO_PROGRESS_TIMEOUT_S` as the lease TTL

## Entry Points

- `shared/host_deploy_state.py:read` / `read_all` — this machine's row / every machine's rows
- `set_posture` / `touch_updater_lease` / `clear_updater_lease` / `updater_lease_live` — the transitions
- `try_acquire_updater_lock` / `release_updater_lock` — the updater mutex

## Notes

- The cluster-level counterpart is [[cluster_lock.ava.okf.md|the cluster deploy lease]] (`deployment_state`); agents carry their own leases in `agents_meta`.
- Gate maintenance ownership is a separate cluster-level fact in [[ui_update_state.ava.okf.md|Cluster UI Update State]]. Host posture transitions never write or clear it.
