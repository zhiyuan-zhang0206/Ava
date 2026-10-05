---
type: doc
title: WAL-G daily tick — base backup, chain verification, guarded retention
description: One OS job runs `ava backup walg run` daily - gates, preflight, backup-push with WALG_DELTA_MAX_STEPS=6, wal-verify read from its JSON status only, and retention that audits WAL-G's dry run against three structural invariants before confirming - and records every step in a 0600 state file the health probe reads.
tags: []
---

# WAL-G daily tick — base backup, chain verification, guarded retention

## What is it
The part of [[walg.ava.okf.md|WAL-G]] that runs once a day. It exists because archived
WAL without a base backup cannot be replayed, and because a backup that is never
verified or pruned either hides a broken chain or fills the bucket. It is one command,
`ava backup walg run`, safe to run by hand at any time and to repeat.

## Core Responsibilities
- **Gates are not failures** (`tick.py`): key unset, another tick holding the per-home
  `run.lock`, an open deploy window and a Postgres that does not answer all end the run
  with exit 0. The last two are recorded as a skip that leaves the last real run's
  outcome alone, so a failure stays visible until a run succeeds.
- **Steps** (`tick.py`): preflight (pinned binary, configuration and key pin, one
  `backup-list`), `backup-push` with `WALG_DELTA_MAX_STEPS=6` (WAL-G itself makes a
  full backup when the chain is six deep and retries a failed one the next day; a
  backup that is not in the list afterwards is a failure), chain verification,
  retention. The first failing step ends the run and is recorded under its name. When
  due, the weekly recovery drill ([[walg-restore.ava.okf.md|restore and drill]]) runs
  between preflight and backup, so that it restores yesterday's backup; its failure is
  recorded in `drill` and never ends the run.
- **Chain verification** (`verify.py`): `wal-verify integrity timeline --json`, only the
  JSON status is read, because WAL-G exits 0 while reporting a gap. `FAILURE` in either
  check fails the run; `WARNING` (segments still uploading) does not; an unknown status
  is an error, never a pass.
- **Guarded retention** (`retention.py`, `backups.py`): `delete retain FULL 3
  --use-sentinel-time` first without `--confirm`. The listed keys are audited into the
  report and must satisfy three invariants: every key is under `basebackups_005/` or
  `wal_005/`; none belongs to the newest backup or the backups it is an increment of
  (the chain is read from the names, `base_<seg>_D_<parent seg>`); a full backup
  survives. Only then the confirmed run, whose deleted set must equal the audited one.
  Any violation aborts with nothing deleted. Nothing is asked of WAL-G until more than
  three full backups exist. No `delete garbage`.
- **State file** (`state.py`): `$AVA_HOME/backups/walg/state.json`, 0600, replaced
  atomically, strict on read: `tick` (latest start, or why it skipped), `run` (latest
  executed run: ok or failed, and the step), `backup`, `verify`, `retention`, and
  `drill` (the latest recovery drill and when one last succeeded). Only the tick (or
  `ava backup walg drill`) writes, under the lock; the probe reads. A corrupt file fails the tick instead of
  being replaced by an empty one.
- **The OS job** (`base/host/system/walg_job.py`): one crontab line (`# ava-walg`) or
  LaunchAgent (`com.ava.walg`) running the command daily, three hours after the logical
  dump hour. Converge (`ensure_walg_job`) keeps it registered exactly while the key is
  set; `ava cluster destroy` removes it; both only act in the default home. Output goes
  to `$AVA_HOME/logs/walg.log`.

## Key Dependencies
- [[walg.ava.okf.md|WAL-G]] — the runner, configuration and key pin the tick uses; the probe that reads the state file
- [[backup.ava.okf.md|PG-Backup]] — the logical dump whose hour the job's time follows

## Entry Points
- `services/backup/walg/tick.py` — `run_tick()`
- `cli/commands/data_plane/walg.py` — `cmd_walg_run()` and the tick lines of `status`
- `cli/commands/converge/_os_jobs.py` — `ensure_walg_job`

## Notes
- Operator procedure and the alert texts: [WAL-G operations](operations.md).
- Retention is the only deleter under the prefix: a bucket lifecycle rule must not expire objects there.
