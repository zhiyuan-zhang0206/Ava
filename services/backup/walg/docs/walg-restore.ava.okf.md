---
type: doc
title: WAL-G restore and weekly recovery drill
description: Restoring a WAL-G backup (ava backup walg restore) fetches it and recovers it to a time, an LSN or the end of the archive with a scratch Postgres whose settings are all launch arguments; the weekly drill runs the same code on the newest backup, recovers to the start of the newest archived segment to prove the WAL chain, and reads a real conversation back.
tags: []
---

# WAL-G restore and weekly recovery drill

## What is it
The recovery half of the physical backup. [[walg.ava.okf.md|WAL-G]] archives WAL and
[[walg-tick.ava.okf.md|the daily tick]] takes base backups; a backup nobody has restored is
not a recovery path, so this node is the code that restores one (`ava backup walg restore`)
and the weekly check that one restores (`ava backup walg drill`, and inside the tick). It is
one code path with two callers.

## Core Responsibilities
- **Restore** (`restore.py`): `backup-fetch` into an empty directory (an increment is fetched
  by name, which resolves its chain), `recovery.signal`, then a scratch postmaster that is a
  direct child of the caller. Every setting is a launch argument and none is written to the
  directory: `archive_mode=off` (a recovering instance must never archive into the live
  chain, even when a backup carries an archive setting), `restore_command` (`wal-g wal-fetch
  %f %p`, `%` in paths escaped), the `recovery_target_*` argument, `recovery_target_action=promote`,
  no TCP listener, a short private socket directory and a free port, and the capacity
  settings `pg_controldata` records for the directory (Postgres refuses to replay WAL from a
  larger primary). It waits for `pg_is_in_recovery()` to be false; a postmaster that exits
  first raises `RestoreError` carrying the end of its log. A directory that is, or contains,
  the home's live data directory is refused.
- **The target proves the chain** (`drill.py`): the drill's target is the *start* LSN of the
  newest archived segment (`pg_stat_archiver.last_archived_wal`), never its end: Postgres
  stops at the first record at or beyond the target, and the first record after a segment's
  end lies in a segment that is not archived yet. A gap, a wrong key or an unreadable segment
  anywhere between the backup and the target ends recovery in Postgres' own FATAL. With no
  segment newer than the backup's end the drill recovers to the end of the archive and
  records no target.
- **The content check**: the promoted copy goes through `verify_restored_database` (the
  logical restore drill's check: required tables, counts, a real agent conversation read back
  through the production checkpoint reader), then `pg_current_wal_lsn()` must be at or beyond
  the target.
- **Scratch space**: `select_throwaway_base(required)`, where `required` is the backup's
  uncompressed size plus the WAL since it started plus `max_wal_size`: no multiplier. The copy
  is removed after native scratch-Postgres shutdown; a failed stop preserves
  its data directory and reports the error. No process-family disappearance proof is made.
- **Schedule and record** (`drill.py`, `tick.py`, `state.py`): due when a backup exists and
  none succeeded for a week minus one tick period (the drill can only run at a tick, so a
  full-week test would slip to eight days). The record (`finished_at`, `ok`, `backup`,
  `target_lsn`, `seconds`, `detail`, `last_ok_at`) is written under the run lock; a failed
  drill keeps the last success and is retried by the next tick; it never stops the backup.
  `ava backup walg drill` runs one now under the same lock.
- **Alerts** (`probe.py`): the latest drill failed; none succeeded within a week plus one
  tick (the drill's only chance to run). Fixed texts, no number.

## Key Dependencies
- [[walg.ava.okf.md|WAL-G]] — the runner, `postgres_command` (the shared archive/restore command builder), the probe
- [[walg-tick.ava.okf.md|WAL-G daily tick]] — runs the weekly drill and owns the state file and lock
- `base/cluster/dataplane/pg_foreground.py`, `pg_throwaway_base.py` — native scratch-postmaster shutdown and base selection
- `scripts/data_plane_ops/restore_drill.py` — `verify_restored_database`, shared with the logical drill

## Entry Points
- `services/backup/walg/restore.py` — `restored_instance()`
- `services/backup/walg/drill.py` — `run_drill()`, `drill_due()`
- `cli/commands/data_plane/walg.py` — `cmd_walg_restore()`, `cmd_walg_drill()`

## Notes
- Operator procedure and the drill's failure texts: [WAL-G operations](operations.md),
  `.agents/skills/ava-guide/operations/references/walg-restore.md`.
- Scope: the database under the same home identity. Rebuilding a lost host's identity state is not covered.
- The directory a restore leaves is a promoted database on a new timeline; before it backs a new
  primary, start a new WAL-G prefix and take a new full backup.

## Restored checkpoint reader

The tick and manual drill receive a required database-for-URL callback from
their entry owner. They pass it unchanged to the restored-checkpoint reader,
which uses the scratch URL and retains that owner's gate and connection
posture. Direct administration, recovery-target checks and scratch teardown
keep their existing ownership. The standalone logical drill captures its own
loaded image after argument parsing and remains subject to database admission.
