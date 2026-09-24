---
type: doc
title: Blocked-round logging
description: Manager and schema pin-blocker diagnostics report the start and bounded heartbeats of a blocked streak; status and mismatch events retain the held-service evidence.
tags: []
---

# Blocked-round logging

## A block's start, heartbeats and end are never silent
`ops/manager.py` logs the first blocked round immediately: which dimension blocked, the scope, the controller's `detail`, and how many **consecutive** rounds the streak has run (WARNING). The streak then repeats on the alarm-bound cadence — `_BLOCKED_ROUND_ALARM_ROUNDS` (10 rounds ≈ 10 min — "no legitimate rollout is longer than this"; `STRANDED_PAUSE_TIMEOUT_S` no longer shares that judgment, since it now bounds only a provably unowned pause) — escalating to ERROR once past the bound, with the running count on every line. The round that clears a streak logs too, so the gap has an end timestamp and the counter resets.

Expected block dimensions (`pause`) never escalate past the first WARNING and their heartbeats drop to INFO: a paused host is the state its operator asked for, and the bounds on pause pathology live in the controllers and hold machinery (`stalled_rollout` ahead of `pause`; the unowned-pause release after `STRANDED_PAUSE_TIMEOUT_S`; the OS hold watchdog). 2026-09-23/24: a pin/schema drift window turned per-round repeats into ~2.9k lines — 79% of one 24h error bucket; the cadence removes that class of noise.

The schema controller also bounds its full pin-blocker ERROR diagnostic: first round, then every ten rounds, with the running count. A changed pin or drift signature starts a new streak; a converged schema check clears it. Quiet rounds still return `DB_DEPENDENT` and the same pin-blocker detail.

Why the block still must not be silent: a skipped round and an all-green round were previously **indistinguishable in the log**, so a Windows runner's 3h07m window with zero roster reconciles (2026-07-28 22:04 → 2026-07-29 01:11, blocked every minute by `Schema ahead of code` while its `ava cluster update` trigger kept failing ~85 times) could only be found forensically, by counting `__main__:_run_check` lines per hour. The start line plus cadence heartbeats keep that gap readable directly.

The cadence is an alarm shape, never a rate limit on the *action* a block retries. Bounding the *action* a block retries — the ~85 failed `ava cluster update` triggers of that window — is a separate mechanism, and a backed-off round still reports its block: [[services/watchdog/block-scope/heal-backoff.ava.okf.md]].

For a schema mismatch, `ops/controllers/schema_mismatch.py` also compares the applied DB
migrations with local code and the cluster pin tree. Each capability watchdog
atomically records its own consecutive schema-blocked rounds and exact
DB-dependent checks held back. The first round, tenth round, and every 60th
round emit `schema_mismatch_blocked` at error level. Host status and the
cluster roster expose the mismatch kind, machine, count, held services, and
recovery detail even when the ops daemon remains online. A gateway whose pin
lacks applied migrations replays the full pin-aware rollout on `ava cluster
update`, including when its own checkout has no newer commit to pull.
