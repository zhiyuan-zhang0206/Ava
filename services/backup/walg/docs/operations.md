# WAL-G operations

WAL archiving ships every completed WAL segment, encrypted, to an OSS prefix
through a pinned WAL-G
([decision](../../../../docs/decisions/2026-10-02-walg-physical-backup.md),
[node](walg.ava.okf.md)). It is off until
`AVA_WALG_CONFIG_FILE` is set; unset, nothing of it runs. It archives WAL and, once a
day, takes a base backup, verifies the archived chain and applies retention;
`ava backup walg restore` recovers one into a directory you name — the end of the
archive, a time or an LSN — and a weekly drill proves that path by restoring the
newest backup into scratch ("The weekly recovery drill" and "Restoring" below).
While it is on, it is the cluster's point-in-time recovery path.

**Files the operator places** (0600, owned by the gateway's OS user):

- The WAL-G configuration JSON, in WAL-G's own format; every key below is required:

  ```json
  {
    "WALG_OSS_PREFIX": "oss://<bucket>/ava-walg/<home label>/pg17/gen1/",
    "OSS_ACCESS_KEY_ID": "...",
    "OSS_ACCESS_KEY_SECRET": "...",
    "OSS_ENDPOINT": "https://oss-cn-shanghai.aliyuncs.com",
    "OSS_REGION": "cn-shanghai",
    "WALG_LIBSODIUM_KEY_PATH": "<path of the key file>",
    "WALG_LIBSODIUM_KEY_TRANSFORM": "hex",
    "WALG_PREVENT_WAL_OVERWRITE": "true"
  }
  ```

  `OSS_REGION` is the bare region id (`cn-shanghai`), not the endpoint's `oss-` form:
  OSS rejects the signature with "Invalid signing region" otherwise, and the
  configuration check refuses an `oss-` value. The prefix names a path (the PG major and a generation number belong in it) and
  cannot sit under `ava-logical/` (the daily dump), `ava-pitr-scratch/` or
  `ava-wsl-cutover-*`. Use a dedicated storage account whose policy is limited to that
  prefix with Get, Put, List and Delete (plus AbortMultipartUpload and ListParts):
  retention deletes, and the logical-dump upload credential must not be able to touch
  the physical chain.
- The libsodium key, 32 random bytes in hex: `umask 077; openssl rand -hex 32 > <key
  file>`. **Losing it makes every archived segment unreadable.** Copy it to two places
  off the gateway host and decrypt-verify a copy before enabling. There is no rotation:
  a new key means a new prefix (a new `gen`).
- A bucket lifecycle rule must not expire objects under the WAL-G prefix: two independent
  deleters break the chain (the daily tick's retention is the only one). Keep any bucket-wide expiry scoped to the other prefixes. Cleaning up abandoned
  multipart uploads can stay bucket-wide.

**Switching it on.** `ava backup walg check` proves the binary, the configuration, the
key and the storage permissions (it writes and deletes one small object under the
prefix). Then:

```bash
ava config set AVA_WALG_CONFIG_FILE=<path of the JSON>
ava stop && ava start          # archive_mode is read only when Postgres is launched
ava backup walg status         # archive_mode=on, failing_now=False, health: ok
ava backup walg run            # the first base backup, supervised (see "The daily tick")
```

Converge installs the pinned binary (`$AVA_HOME/runtime/walg/wal-g`; Linux x86_64 only,
anything else fails here), validates the configuration and records the key fingerprint
in `$AVA_HOME/backups/walg/key-id` before Postgres starts, so a bad configuration fails
`ava start` instead of producing a Postgres whose archive command can never succeed. A
plain update retains the running Postgres and does not activate or deactivate
archiving: `ava start` prints a warning when the running Postgres differs, and the
health probe reports it. After a start, `SELECT pg_switch_wal();` and an increase of
`archived_count` in `pg_stat_archiver` show a segment arriving in the bucket.

**The daily tick.** `ava backup walg run` executes the supervised backup,
verification and retention path described by the [daily tick owner](walg-tick.ava.okf.md).
The registered daily job writes `$AVA_HOME/logs/walg.log`. Run the first backup
by hand and inspect it: its duration and upload volume are not established for a
new bucket. A concurrent run stands down; a deployment or unavailable Postgres
can produce a deliberate skip. A skip is not evidence of a successful backup.

`ava backup walg status` reads the tick's recorded backup, verification, retention
and recovery-drill results. Inspect the last executed result and `walg.log`, not
only the scheduler's exit code. Do not manually delete objects in the physical
prefix: the tick's guarded retention is its sole deletion path. The tick owner
specifies increment depth, retained full backups and deletion invariants.

**The weekly recovery drill.** A backup nobody has restored is not a recovery path, so the
tick restores one every week, before that day's backup (it therefore restores yesterday's:
the one a lost host would need). The drill is due one tick period before a week since the
last success, and a failed drill is retried by the next tick. It fetches the newest backup
into a scratch directory (`AVA_PG_THROWAWAY_BASE`, else the platform default, else the disk
fallback; room is the backup's uncompressed size plus the WAL since it started plus
`max_wal_size`, and a base that cannot hold that is refused), recovers it with a scratch
postmaster to the start of the newest archived WAL segment, and reads a real conversation
back through the checkpoint reader. Reaching that point proves every segment between the
backup and it is in the bucket, decrypts and replays: a missing one ends recovery in
Postgres' own FATAL, which the record carries (`ava backup walg status`, "last drill").
The scratch copy and its process are removed in every outcome, and a failed drill never
stops the backup that follows. `ava backup walg drill` runs it now (same lock as the tick);
after switching WAL-G on, run it by hand once after the first `run` and check the result
before trusting the schedule. Its download saturates the downlink for roughly the length of
a base-backup fetch (an estimate, not measured on this bucket): schedule hand runs
accordingly.

**Restoring** (`ava backup walg restore --dir <empty directory> [--backup NAME]
[--user <superuser>] [--time 'YYYY-MM-DD HH:MM:SS+00' | --lsn X/X]`). It never touches this home's data
directory (a `--dir` that is or contains `$AVA_HOME/pg` is refused) or its ports. Steps:

1. Pick the backup. `LATEST` is the default; to recover to a time or LSN, name the newest
   backup that *started* before it (`wal-g backup-list --detail` shows start times; an
   increment resolves its whole chain by name). `LATEST` can name a backup newer than the
   target, which cannot be recovered to it.
2. Run the verb on a host with the same Postgres major (17), the same extensions
   (pgvector), the configuration JSON and a copy of the encryption key. Without `--time`
   or `--lsn` it recovers to the end of the archive. It fetches the backup, writes
   `recovery.signal`, and starts a scratch postmaster on that directory (unix socket only,
   its own temporary socket directory and port, `archive_mode=off`, the capacity settings
   `pg_controldata` records, `restore_command` = `wal-g wal-fetch %f %p`) until it is
   promoted, then shuts it down cleanly. The directory is left as a promoted database on a
   new timeline; it is not started.
   `--user` names the restored cluster's superuser, the OS user that ran initdb on the
   source (default: the current OS user); a role Postgres refuses ends the restore at once.
3. A missing segment, a wrong key or an unreachable target ends in Postgres' FATAL, printed
   with the end of its log; the directory is left for inspection, and must be emptied
   before a retry.
4. Before this database replaces the live one or backs a new primary: **use a new WAL-G
   prefix (a new "generation", e.g. `.../gen2/`) and take a new full backup first.** The
   recovered database is on a new timeline; archiving it into the old prefix would mix two
   histories there.

Scope: this recovers the **database** under the same home identity. A whole-host rebuild
(the `$AVA_HOME/db-authority/` ledger, `.env`, `start-intent.json`) is not covered or
exercised; see [disaster recovery](../../../../docs/conventions/disaster-recovery.md).

**Switching it off.** `ava config unset AVA_WALG_CONFIG_FILE`, then `ava stop && ava
start`; converge removes the daily job. Nothing is left in the data directory (the archive
settings are launch arguments). The objects stay in the bucket; deleting them is a manual storage action.
There is no way to stop archiving without a restart: when archiving misbehaves, repair
its cause (credentials, network, disk).

**Health and investigation.** The [archiving owner](walg.ava.okf.md) specifies
all probe states, thresholds and skip semantics. For a reported failure, inspect
`ava backup walg status`, the end of `walg.log`, the archiver state and the pinned
key before acting. A broken chain cannot recreate missing segments: take a new
full backup and treat the older affected recovery points as lost. A skipped tick
does not clear an earlier executed failure. Repair the archive cause rather than
changing the pinned key under an existing prefix or deleting state to hide it.
