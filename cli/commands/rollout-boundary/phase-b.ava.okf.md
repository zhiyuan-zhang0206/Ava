---
type: doc
title: Phase B Runner Convergence
description: The native readiness and code evidence required before a runner update can report success.
tags:
- cli
- operations
- rollout
---

# Phase B Runner Convergence

- `update.py`'s Phase-B poll answers a `PollVerdict` per host — the stall verdict
  requires an existing idle `host_deploy_state` row with no live updater lease,
  an explicit `paused=false` status snapshot, and nonempty matching source and
  responding-process SHAs. Both SHAs must equal the dispatched target when one
  exists. The status producer includes native admission hold and incomplete
  `start-serving` readiness in `paused`; business HTTP admission keeps its
  separate DB-posture policy. A same-code restart therefore cannot pass on the
  idle row and old serving marker retained through Phase A. Missing evidence is
  never success. The running SHA speaks for the answering ops daemon, not every
  sibling service. Resume can precede updater lease cleanup; an idle row with a
  live lease keeps waiting even if an old terminal log remains.
  Non-idle rows retain the lease/stall rules (live lease → working;
  paused+expired / converging+no-lease → STALLED ×2), and the no-progress
  verdict (P1, 2026-08-30) reads the probe's own stage evidence: two consecutive
  probes naming a `current_stage` in flight beyond `STAGE_NO_PROGRESS_TIMEOUT_S`
  — the last `t=` marker the updater printed at the stage's entry and its age on
  the host's monotonic clock — return `POLL_NO_PROGRESS` even while the lease is
  still live, because a lease is one write at the run's start and cannot speak
  for progress. The same bound and evidence drive the host's own hung-updater
  reaper, whose kill now also clears the updater lease. A POLL_* status plus the
  `last_updater_outcome` the runner reported on the probe that settled it
  (`ops.updater_outcome`, read off that host's own updater log and anchored to
  its pause flag so a previous update's log is reported as *no record* rather
  than as this one's; on Windows, where the supervisor appends every run to ONE
  log, that flag anchors twice — the updater echoes a per-run start marker and
  the tail is sliced at it, so a previous run's decline is not read as this
  run's verdict). The status alone stops one level short of what an operator
  needs: `POLL_STALLED` covers both a preflight that refused (nothing stopped,
  host still serving its old code) and an updater that died after moving the
  checkout, and those want opposite next actions. It carries no extra dial —
  the probe that proves the host stopped is the probe that says why — and
  changes no deploy behaviour. A refusal is told to re-run rather than to wait:
  its own watchdog cannot clear it.
- Phase B owns its retry loop: every outer poll performs exactly one
  `status_probe` RPC (`retries=0` at the cluster-RPC layer). Per-attempt logs
  carry only machine, ordinal, outcome and duration; each host also prints one
  terminal elapsed/probe-count line. This keeps a two-second probe from nesting
  four transport attempts and makes the slow host identifiable without logging
  the full status payload.
- Phase B's per-host **absolute** deadline is 900 seconds, the shared
  `PHASE_B_ABSOLUTE_TIMEOUT_S` no-progress bound. C3's 300-second value is not a
  competing deadline: it hands a host with continuous, evidenced progress to the
  settle hold early, while stalled and no-progress verdicts remain faster exits.
