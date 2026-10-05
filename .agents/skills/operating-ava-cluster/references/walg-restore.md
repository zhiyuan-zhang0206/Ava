# WAL-G physical restore and the weekly recovery drill

Use this to recover the database from the WAL-G physical backup (to a time, an LSN or
the end of the archive), or to read the weekly drill's result. The logical dump is a
separate path: [`db-restore.md`](db-restore.md). Operator detail and the alert texts:
`docs/conventions/runbook.md`, "WAL-G archiving". Never restore into a live data directory.

## Read the drill first

```bash
ava backup walg status      # "last drill" and "last successful drill", health line
ava backup walg drill       # run one now (same lock as the daily run); exit 0 only if it passed
```

A passing drill means: the newest backup was fetched and decrypted, every WAL segment from
it to the start of the newest archived segment replayed, and a real conversation read
back. Its record names the backup, the target LSN (`the end of the archive` when nothing
newer than the backup had been archived), the duration and, on failure, Postgres' own error.

| What the drill detail says | Meaning and next step |
| --- | --- |
| `recovery failed ... recovery ended before configured recovery target was reached` | A segment between the backup and the target is missing or unreadable. `wal-g wal-verify integrity timeline --json`; if a segment is lost it cannot be repaired: take a new full backup and treat older recovery points as lost. |
| `recovery failed ... max_connections` (or another capacity setting) | The recovering instance was smaller than the source. The restore copies the control file's values; this means `pg_controldata` was bypassed: report it. |
| `backup-fetch failed` | Credentials, key or prefix. `ava backup walg check`. A wrong key reads as "corrupted chunk". |
| `restored schema is missing ...`, `no readable agent conversation` | The restore worked but the content check did not: the database restored is not the application database. Do not trust the backup until explained. |
| `... exists and is not an empty directory`, `... live data directory` | Not a drill result: a restore was pointed at a bad directory. |
| scratch space refused | `AVA_PG_THROWAWAY_BASE` or the platform default cannot hold the backup plus WAL; free space or point it at a larger volume. |

## Restore

1. Choose the backup: for a target time or LSN, the newest backup that started before it
   (`LATEST` may be newer than the target).
2. On a host with the same Postgres major (17), pgvector, the configuration JSON and the
   encryption key:

   ```bash
   ava backup walg restore --dir /srv/restore/pg --backup <NAME> --time '2026-10-01 12:04:57+00'
   ava backup walg restore --dir /srv/restore/pg --lsn 0/3000060
   ava backup walg restore --dir /srv/restore/pg            # newest backup, end of the archive
   ```

   Run as an OS user other than the one that ran initdb on the source, add `--user <that
   user>`: it is the restored cluster's superuser, and a role that does not exist ends the
   restore at once with Postgres' error.
3. The directory is left as a promoted database on a new timeline, Postgres not running.
   A failure leaves the directory for inspection; empty it before retrying.
4. Before it replaces the live database or backs a new primary, switch to a new WAL-G
   prefix (a new generation) and take a new full backup: the recovered database is on a
   new timeline and must not archive into the old prefix.

Scope: the database under the same home identity. Rebuilding a lost host's identity state
(`$AVA_HOME/db-authority/`, `.env`, `start-intent.json`) is not covered.
