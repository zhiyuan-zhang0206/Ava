---
type: doc
title: Stranded deploy-lease reclamation — the automatic counterpart of `ava cluster recover`
description: How the watchdog reclaims a dead orchestration's cluster deploy lease in one round — the positive-death evidence it requires, the live-signal gates, the compare-and-set clear, and what it deliberately leaves to hand recovery.
tags: []
---

# Stranded deploy-lease reclamation

A killed orchestration cannot release the cluster deploy lease
(`shared.cluster_lock`, the single `deployment_state` row), and the row then
refuses every new deploy for up to `LOCK_TTL_S` (30 min) on the strength of a
process that is gone. The 2026-09-12 dev-worktree incident is the shape: a
rollback died mid-start, its lease blocked the next deploy, and the wait ended
9.5 minutes later with a hand-run `ava cluster recover`. The reclaim lives in
`ops/controllers/stranded_lease.py`, as the `lease` controller — registered
ahead of the pause gate (a killed rollout strands the lease while its hosts are
paused, and everything behind `pause` is short-circuited away) and never
blocking, on both capabilities.

## The bounds it accepts

- **A plain executing lease only** (`note is None`). A settle hold's whole
  purpose is to outlive its writer — it is released by convergence or its own
  TTL, and is never touched here.
- **Positive local-death evidence only** (`shared.cluster_lock.
  holder_process_gone`): this machine's holder, pid absent — or aged past the
  pid-recycling slack and provably recycled. It is the negation of the probe
  `ops.ops_cluster._lock_holder_is_live` uses, so the manual and automatic paths
  can never disagree; a holder on another machine, an unparseable holder string,
  and an unreadable process identity all read as live.
- **No live local signal**: a local orchestration session or a live updater
  lease declines (the live process owns the row).
- **A compare-and-set clear** on the exact lease observed
  (`claim_recovery_lock`), so a racing new owner is never clobbered.

## What it leaves alone

The paused posture and a maintenance hold are not touched — a hold stays
hand-recovered (the pause controller declines an explicit maintenance hold, and
the recovery recipe lives in `conventions/graceful-maintenance.md` and the
runbook's interrupted-rollout section). Reclaiming the lease only removes the
deployability block, so the next sanctioned recovery (operator, `ava start`, or
the next rollout) is no longer refused by a corpse's lease. See
[[services/watchdog/pause-recovery/pause-recovery.ava.okf.md]] for the pause
half of the same incident.
