# Partial restore from the physical backup

Recovering a table — or a set of rows — out of the WAL-G physical backup while
the live database keeps serving: a `DELETE`, a `DROP` or an `UPDATE` went wrong
and the goal is to put only that data back, not to restore the cluster. The
whole-database paths are separate documents:
[`walg-restore.md`](../../../ava_builtins/skills/platform/ava-guide/operations/references/walg-restore.md)
(physical restore to a time, an LSN or the end of the archive) and
[`db-restore.md`](../../../ava_builtins/skills/platform/ava-guide/operations/references/db-restore.md)
(the encrypted logical dump).

**Never restore into the live database, and never point a recovered copy at the
live cluster.** Every step below reads the backup and works in a scratch
directory; the single write against the live database is the final load in
section 6, under the same discipline as any production change.

## 1. Contain, and pick the recovery target

1. Stop the damage if it is still happening: stop the writer, revoke the role or
   stop the service that runs the offending statement. Do not restart anything
   that could replay it.
2. Write down the incident time in **UTC** (`date -u`). The recovery target must
   be a moment **before** the offending transaction; when the statement time is
   known, aim a few seconds before it. Transactions committed after the target
   are not in the recovered copy.
3. Keep the live database authoritative for everything else: rows written after
   the target exist only there, and the load-back in section 6 must not
   overwrite them.

## 2. Pick the backup

The target decides the backup: use **the newest backup that *started* before
the target** — `LATEST` may name a backup that started after it, which cannot be
recovered to it. An increment (a name of the form `<base>_D_<parent>`) resolves
its whole chain when fetched, so a target inside the newest increment's window
is served by that increment.

```bash
wal-g --config <the configuration JSON> backup-list --detail
```

The configuration JSON is the one `AVA_WALG_CONFIG_FILE` names; on the gateway
host it is the file the archiving runbook places. The `start_time` column of
`backup-list --detail` is what must be before the target, not `finish_time`.

## 3. Restore a scratch copy to the target time

Run this on a host with the same Postgres major, pgvector, the configuration
JSON and the encryption key — in practice the gateway host, where the restore
verb already works for the weekly drill:

```bash
ava backup walg restore \
  --dir /srv/restore/agent_tasks-2026-10-03 \
  --backup <backup name from section 2> \
  --time '2026-10-03 04:30:00+00'
```

- `--dir` must be an empty directory (or one that does not exist yet). The verb
  refuses a directory that is, or contains, the home's live data directory, and
  it never touches the live ports.
- The directory is left as a promoted database on a new timeline, Postgres not
  running. A failure leaves the directory for inspection — empty it before a
  retry.
- The download saturates the downlink for roughly the length of a base-backup
  fetch; schedule hand runs accordingly.
- Measured at production scale (2026-10-03, newest increment + one time target
  on the main cluster's ~18 GiB chain): full-chain fetch `711 s`, WAL replay
  and promotion `149 s` (promoted at `860 s`, new timeline 2), end-to-end drill
  including the content check `877 s`. Record:
  `infra/backup/walg-restore-drill-20261003` in the memory pool.

## 4. Start the copy for inspection

The restore leaves Postgres stopped, so start it the way the scratch postmaster
ran — no TCP listener, a private socket directory, archiving off:

```bash
sock=$(mktemp -d /tmp/restore-inspect-XXXX)
/usr/lib/postgresql/17/bin/pg_ctl -D /srv/restore/agent_tasks-2026-10-03 \
  -o "-p 55444 -c listen_addresses= -c unix_socket_directories=$sock -c unix_socket_permissions=0700 -c archive_mode=off" \
  -l "$sock/postgres.log" start
psql -h "$sock" -p 55444 -d ava_main
```

On macOS resolve the same-major binary through the host's installation (the
vendored tree or `$(brew --prefix postgresql@17)/bin`). The copy authenticates
as the OS user that owns it (`peer` on the private socket), and it must never
join the live cluster; its WAL must never reach the live WAL-G prefix — it is a
new timeline. Stop it with `pg_ctl -D /srv/restore/agent_tasks-2026-10-03 stop`
when done; remove the directory afterwards.

Sanity-check the point in time before extracting:

```bash
psql -h "$sock" -p 55444 -d ava_main \
  -c "SELECT count(*), max(created_at) FROM agent_tasks;"
```

`max(created_at)` should sit at or before the recovery target, and the rows you
are about to recover should be present. (The start / query / extraction /
clean-stop sequence above was exercised on a production-scale copy on
2026-10-03.)

## 5. Extract the data

Whole table (the task registry's table is `agent_tasks`; list the tables with
`\dt` when unsure):

```bash
pg_dump --format=custom --table=agent_tasks --file=/srv/restore/agent_tasks.dump \
  "postgresql://<os-user>@/ava_main?host=$sock&port=55444"
pg_restore --list /srv/restore/agent_tasks.dump    # must name TABLE DATA public agent_tasks
```

Selected rows, when only part of the table was lost (CSV keeps the extract
reviewable):

```bash
psql -h "$sock" -p 55444 -d ava_main \
  -c "\copy (SELECT * FROM agent_tasks WHERE updated_at < '2026-10-03 04:30:00+00') TO '/srv/restore/agent_tasks.csv' WITH (FORMAT csv, HEADER)"
```

## 6. Load back into the live database

This is the one production write. Announce it, make sure a fresh recovery point
exists (the daily logical dump plus the WAL-G chain both cover it), and prefer
loading into a staging table when only rows are missing:

- **Whole table replaced.** `TRUNCATE public.agent_tasks;` then
  `pg_restore --data-only --table=agent_tasks --dbname=<live URL> /srv/restore/agent_tasks.dump`,
  then fix the sequence: `SELECT setval(pg_get_serial_sequence('agent_tasks','id'),
  (SELECT max(id) FROM agent_tasks));`
- **Rows only.** Load the extract into a staging table
  (`CREATE TABLE agent_tasks_recovered (LIKE agent_tasks INCLUDING ALL);` and
  `\copy agent_tasks_recovered FROM '/srv/restore/agent_tasks.csv' WITH (FORMAT csv, HEADER)`),
  review the diff against `agent_tasks`, then insert with an explicit column
  list and `ON CONFLICT` handling, and drop the staging table.

Loading extracted rows back is a normal SQL write, not a restore: the
prohibition is on pointing restore tooling or a whole data directory at the
live cluster. The write is archived by WAL-G like any other; it does not
disturb the chain.

## 7. Verify and record

- Row counts and the recovered range match the extract; spot-check a row
  through the application (a task, a conversation) rather than only in SQL.
- Record: incident time, target, backup name, the times measured, what was
  loaded, and where the scratch copy was left (or that it was removed).

## Pitfalls

- `--time` is UTC (`+00`); a local-time target silently shifts the window.
- The newest backup may have STARTED after the target — take the previous one.
- A failed restore leaves a partial directory; it must be emptied before retry.
- The scratch copy is a new timeline; do not let it archive anywhere and do not
  reuse it as a primary.
- Check the extensions on the restoring host (pgvector is required by this
  database) and the disk headroom — the copy needs the backup's uncompressed
  size plus the WAL since it started plus `max_wal_size`.
