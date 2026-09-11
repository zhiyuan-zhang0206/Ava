---
type: doc
title: Stranded-hold recovery — the bounded completion of an update-armed hold
description: The three `host_deploy_state` budget columns and the one bounded automatic completion they bound (task #3142) — which holds may be completed (update-armed, post-stop, never from the gateway capability's round), what the completion runs (the same stop/start/resume an operator runs), the compare-and-set that makes it once-per-episode, and the kill-switch.
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
- `cli/commands/_hold_recover.py` — the detached session's entry
  (`python -m cli.commands._hold_recover --operation … --acquired-at …`). It
  re-verifies the hold's exact `(holder, acquired_at)` generation, its phase,
  the verdict and the switch, then runs: for `stopping`, the stop the update leg
  itself runs (`cli.commands.stop._do_stop` with `keep_infra` / terminals /
  browser retained, this home's declared services only — which is why the
  recovery session and the browser survive it); then `maintenance start`
  (`cli.commands._maintenance._start`); then `maintenance resume` (`_resume`).

## Boundaries

- **Never automatic**: an operator hold, a pre-stop phase (`preparing` /
  `draining` / `drained` — an incomplete drain is `resume --cancel` work), a
  `ready` hold, any state with a live owner, an unreadable round, and the
  gateway capability's watchdog round. The last one is the conservative v1 cut:
  the gateway watchdog never initiates a completion, and the gateway-only
  deployment (which runs no agent-runner watchdog) waits for an operator. A
  unit that also serves `agent-runner` — macmini, WSL — completes its own hold
  in that capability's round.
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
- [[../maintenance.ava.okf.md|maintenance]] — the hold, its phases, and the
  `authorized_start` boundary the start/resume legs run inside
- `conventions/graceful-maintenance.md` — the operator recipe this automation
  mirrors (per-phase manual steps; a failed attempt is taken over by hand from
  wherever it stopped)

## Entry Points

- `shared/host_deploy_state.py:reserve_stranded_recovery` /
  `finish_stranded_recovery` — spend one attempt / record its outcome
- `ops/hold_recovery.py:spawn_hold_recovery` — start the detached completion
  session
- `cli/commands/_hold_recover.py:_main` — the session's entry point
