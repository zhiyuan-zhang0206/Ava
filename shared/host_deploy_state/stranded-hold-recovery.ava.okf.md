---
type: doc
title: Stranded-hold recovery — the bounded completion of an update-armed hold
description: The `host_deploy_state` budget columns and the two bounded automatic completions they bound (task #3142's update-armed post-stop completion, task #3270's abandoned pre-stop release) — which holds each path releases, what it runs, the once-per-episode compare-and-set, and the kill-switches.
tags:
- deploy
- recovery
- r1
---

# Stranded-hold recovery

## What it is

The stranded-hold record (task #3132, [[host_deploy_state.ava.okf.md|Host Deploy
State]]) makes a maintenance hold whose owner died loud. This node covers the
one *resumable* shape of that record: an **update-armed** hold (the pause
window's updater run reads FAILED — the record's own evidence) at a **post-stop**
phase (`stopping` / `stopped` / `starting`), on a unit that is **not the
gateway**. Since 2026-09-12 the pause controller completes that shape once, on
its own, through the same legs an operator runs by hand — the narrow exception
approved as decision D1, bounded by three columns on the row that already
carries the record:

- `stranded_hold_attempts` — attempts this episode has consumed;
- `stranded_hold_attempted_at` — written by Postgres' `now()`, the clock the
  900s cooldown reads;
- `stranded_hold_recovery_note` — the latest attempt's outcome text for the
  operator (the reservation seeds it, the executor overwrites it).

All three reset with the record (`mark_stranded_hold` resets them when it stamps
a *new* episode's `since`; `clear_stranded_hold` wipes them), so every episode
starts with a fresh budget and a spent one is never refunded.

Task #3270 added the sibling path for the **pre-stop** phases (`preparing` /
`draining` / `drained`, where `resume --cancel` is legal): an ownerless,
failure-free hold whose recorded shepherd is gone (a dead process, or an
update-armed leg whose updater died before the stop) is declared `abandoned`
at the 600s notice bound, and the pause watchdog cancels it after a 1800s
window.

## Core Responsibilities

- `reserve_stranded_recovery` (in `shared/host_deploy_state.py`) — the
  compare-and-set that IS the bound: one conditional `UPDATE ... RETURNING`
  against the budget and the cooldown, so two racing deciders cannot both spawn
  a completion leg, and the winner's attempt number comes back. A declined
  reservation means no action (budget spent, inside the cooldown, or the record
  cleared between the verdict and the reservation).
- `ops/hold_recovery.py` — the policy constants (`MAX_ATTEMPTS = 1`,
  `COOLDOWN_S = 900`, `RECOVERABLE_PHASES`) and `spawn_hold_recovery`, which
  starts the detached `ava-hold-recover` session through the same
  `cluster_session._spawn_detached_session` mechanism the updater / rollout
  sessions use (its service slug lives in `ops/cluster_session.py`, and
  `shared.proc` sanctions the session as a host of an in-process host
  transition). A spawn that fails still spends the attempt.
- `ops/controllers/stranded_pause.py:maybe_spawn_stranded_recovery` — the gate:
  a `stranded` verdict, a post-stop phase, not the gateway capability's round,
  the kill-switch on, then the reservation. Called from the pause controller's paused branch
  (the only local actor while the host is paused).
- `shared/hold_driver.py` — the recorded shepherd the proof reads: the topmost
  non-relay process below the writer's session leader (`sh -c` relay shims are
  never the owner; fallback: the direct parent). `missing`/`unreadable` never
  license a release.
- `ops/strand_hold.py:maybe_release_abandoned_hold` — the release gate (the
  switch defaults on): window plus whole-proof re-verification under the
  lifecycle lock; a failed release keeps the hold and re-marks the record.
- `ops/cluster_pause.py:release_pre_stop_hold` — the cancel-parity release:
  pre-stop only, no failed receipts, the host-proof and data-plane
  reachability cancel demands, then `authorized_start` + unpause, ERROR-audited.
- `cli/commands/_hold_recover.py` — the detached session's entry
  (`python -m cli.commands._hold_recover --operation … --acquired-at …`). It
  re-verifies the hold's exact `(holder, acquired_at)` generation, its phase,
  the verdict and the switch, then runs: for `stopping`, the stop the update leg
  itself runs (`cli.commands.stop._do_stop` with `keep_infra` / terminals /
  browser retained, this home's declared services only — which is why the
  recovery session and the browser survive it); then `maintenance start`
  (`cli.commands._maintenance._start`); then `maintenance resume` (`_resume`).

## Boundaries

- **Never automatic**: any state with a live owner, an unreadable round, a
  `ready` hold — and, for the #3142 completion, every gateway-capability
  watchdog round (the conservative v1 cut: the gateway watchdog never
  initiates a completion, so the gateway-only deployment waits for an
  operator; a unit that also serves `agent-runner` — macmini, WSL — completes
  its own hold in that capability's round). The #3270 release is narrower:
  pre-stop, empty failures, gone shepherd; it runs from the pause watchdog
  round and keeps the operator's window first. Failure-carrying holds
  (repair first), legacy journals without a recorded shepherd, and unreadable
  probes stay declaration-only or manual on both paths.
- **Bounded**: one attempt per episode, 900s cooldown, and a kill-switch —
  `settings.gateway.stranded_hold_recovery` (`AVA_STRANDED_HOLD_RECOVERY`,
  default on), read by the watchdogs every tick (a change applies at their next
  restart; the field's restart hint is the runner-side `ops` daemon).
- **Observable**: the spawn is logged by the pause controller, the outcome lands
  in `stranded_hold_recovery_note`, and the full run is teed to
  `$AVA_HOME/logs/hold-recover-<epoch>.log` (the updater family's log rotation).
  The task #3132 alarm and roster banner keep standing until the record clears —
  a completed attempt clears it through the ordinary verdict path, a failed one
  leaves both loud.

## Key Dependencies

- [[host_deploy_state.ava.okf.md|Host Deploy State]] — the row, the record, and
  the budget columns
- [[../maintenance/maintenance.ava.okf.md|maintenance]] — the hold, its phases, and the
  `authorized_start` boundary the start/resume legs run inside
- [[../pause_owner.ava.okf.md|Deploy pause owner]] — the journal that carries
  the hold and the recorded shepherd
- `conventions/graceful-maintenance.md` — the operator recipe this automation
  mirrors (per-phase manual steps; a failed attempt is taken over by hand from
  wherever it stopped)

## Entry Points

- `shared/host_deploy_state.py:reserve_stranded_recovery` /
  `finish_stranded_recovery` — spend one attempt / record its outcome
- `ops/hold_recovery.py:spawn_hold_recovery` — start the detached completion
  session
- `cli/commands/_hold_recover.py:_main` — the session's entry point
- `ops/strand_hold.py:maybe_release_abandoned_hold` — the #3270 pre-stop
  release (window + re-verification)
- `ops/cluster_pause.py:release_pre_stop_hold` — the cancel-parity release
- `shared/hold_driver.py:mint_driver` / `liveness` — record / judge the shepherd
