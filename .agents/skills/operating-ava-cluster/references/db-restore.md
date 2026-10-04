# Encrypted database restore drill

Use this procedure to prove that a managed Postgres backup can recover an Ava
cluster. The artifact contains the entire database, including LangGraph
checkpoint tables: those tables are the only copy of conversation history.
Never restore an artifact into the live database.

## Recovery objectives

- **RPO:** one day at the daily cluster-time backup window (`services.backup_hour`, default 03:00).
- **RTO target:** complete a full restore within one maintenance window.
- **Measured dry run (2026-08-25):** the test-sized encrypted artifact completed
  the full decrypt → gunzip → scratch restore → checkpoint-reader verification
  path in 22.8 seconds. This is a command-path measurement, not a substitute
  for the operator's production-sized drill; record that elapsed value here
  after it runs.
- **Measured dry run (2026-08-27, production size):** the new-format artifact
  (677 MiB, custom+zstd dump of the 4.34 GiB DB) completed decrypt → scratch
  restore → checkpoint-reader verification in **136.6 s** (`agents=3527
  checkpoints=4345 checkpoint_blobs=10334 sample_agent=405 messages=14041`).
- **Artifact format (2026-08-27 change):** artifacts are now
  `<db>-<utc>.dump.enc` — a custom-format `pg_dump` (zstd-compressed in-dump)
  encrypted with AES-CBC, with no separate gzip layer. The drill's gunzip step
  is gone; a legacy `<db>-<utc>.dump.gz.enc` artifact is detected by its gzip
  magic header and decompressed automatically, so old artifacts remain
  restorable through the same procedure.

## Recommended automated drill

From the checkout that owns the backup cluster, run either command:

```bash
.venv/bin/python scripts/data_plane_ops/restore_drill.py
.venv/bin/python scripts/data_plane_ops/restore_drill.py /absolute/path/to/<db>-<utc>.dump.enc
```

The first command selects the newest managed local artifact. The script creates
a native throwaway Postgres cluster, decrypts the artifact (decompressing a
legacy gzip layer when present), restores with `pg_restore --clean
--if-exists`, and removes all scratch data and the cluster when it exits.

The drill picks the scratch cluster's base directory with capacity awareness.
The platform default (`/dev/shm` on Linux) is kept when the estimated restore
fits; a restore larger than the tmpfs demotes to the disk fallback (`/var/tmp`
where present). Set `AVA_PG_THROWAWAY_BASE` (absolute path, unit `.env`) to
force a base. The drill logs the base it uses, and a failing `pg_restore` names
that base and its free space — if the server closed the connection mid-copy
(`PQputCopyData`), the scratch base ran out of room: free space on it, or point
`AVA_PG_THROWAWAY_BASE` at a larger volume.

A successful run prints this shape (counts vary by artifact):

```text
restore drill passed: agents=567 checkpoints=5677 checkpoint_blobs=7971 checkpoint_writes=5680 sample_agent=42 messages=18 elapsed_seconds=381.4
```

Expected facts:

- `agents`, `checkpoint_blobs`, `checkpoints`, and `checkpoint_writes` are all
  present in the restored schema and their counts are printed.
- `sample_agent` names a restored checkpoint thread; `messages` is read through
  `base.agents.history.checkpoint.load_checkpoint_messages_full`, not raw table bytes.
- The successful checkpoint-reader call is the service smoke: it proves the
  restored LangGraph schema and serialized conversation data are usable.

## Manual transform reference

The automatic script is preferred because it owns throwaway-Postgres cleanup.
For an operator investigating an artifact, the transform it performs is:

```bash
scratch_dir=$(mktemp -d)
chmod 700 "$scratch_dir"
key_file="$scratch_dir/backup.key"
.venv/bin/python -c 'from services.backup.artifact.passphrase import logical_backup_passphrase; print(logical_backup_passphrase())' > "$key_file"
chmod 600 "$key_file"
openssl enc -d -aes-256-cbc -pbkdf2 -salt -kfile "$key_file" -in /absolute/path/to/<db>-<utc>.dump.enc -out "$scratch_dir/backup.dump"
chmod 600 "$scratch_dir/backup.dump"
# Legacy artifacts (<db>-<utc>.dump.gz.enc) need one extra step:
# gzip --decompress --stdout "$scratch_dir/backup.dump" > "$scratch_dir/backup.dump.raw" && mv "$scratch_dir/backup.dump.raw" "$scratch_dir/backup.dump"
```

The key file holds the logical-backup passphrase from its one resolution
(`services/backup/artifact/passphrase.py`, the same one every backup and
restore uses): the pinned `$AVA_HOME/backups/logical-backup.passphrase`. A
gateway birth mints it; a home born earlier carries `sha256(secret)`, pinned
once. It never changes with the cluster secret and is never
derived: a home without it refuses. It is private, never passed on argv, and
must be deleted with the scratch directory after the drill. Only the gateway
holds it, so disaster recovery needs that file: keep an escrowed copy with the
gateway's other backup keys. An artifact an empty-secret home wrote before its
cutover pinned a minted passphrase was encrypted under the public
`sha256("")`; restore it with
`.venv/bin/python scripts/data_plane_ops/restore_drill.py <artifact> --legacy-empty-secret-passphrase`
(by hand: the key file holds `printf '' | shasum -a 256 | cut -d' ' -f1`).
The archive's compression CRC and `pg_restore` failure path detect corruption;
the artifact is encrypted with AES-256-CBC and inherits the local artifact's
0600 threat model.

To complete a manual investigation, use a scratch Postgres URL only:

```bash
pg_restore --clean --if-exists --dbname="$SCRATCH_DB_URL" "$scratch_dir/backup.dump"
psql "$SCRATCH_DB_URL" -c "SELECT to_regclass('agents'), to_regclass('checkpoint_blobs'), to_regclass('checkpoints'), to_regclass('checkpoint_writes');"
psql "$SCRATCH_DB_URL" -c "SELECT count(*) AS agents FROM agents;"
psql "$SCRATCH_DB_URL" -c "SELECT count(*) AS checkpoint_blobs FROM checkpoint_blobs; SELECT count(*) AS checkpoints FROM checkpoints; SELECT count(*) AS checkpoint_writes FROM checkpoint_writes;"
```

All four `to_regclass` values must be non-null. The counts must be plausible
for the artifact's backup time. Finish with the automated drill when a readable
checkpoint conversation must be proved.

## Off-site encrypted copy

After local encryption succeeds and before local pruning, the gateway publishes
the `.dump.enc` artifact to Aliyun OSS under the `ava-logical/` namespace
(`services/backup/artifact/offsite.py`). It needs
`AVA_BACKUP_OFFSITE_ENDPOINT`, `AVA_BACKUP_OFFSITE_BUCKET` and
`AVA_BACKUP_OFFSITE_CREDENTIALS_FILE` (set through `ava config set`); a home
without all three skips the leg with one INFO log line. The publish is
if-absent (server-enforced `x-oss-forbid-overwrite` on completion; the bucket
must stay versioning-off) and verified (per-part `Content-MD5` plus the
multipart ETag chain). It is optional: a failed publish or an unusable
credentials file logs the cause but never discards the local artifact — the
local copy remains the primary. Success is judged by the destination, not by
silence: the log line `[backup] off-site published
ava-logical/<name> (size=..., pin=..., checksum=md5:...)` and the object itself,
its size equal to the local `.dump.enc`. To publish one existing artifact by
hand: `python -m services.backup.dump --publish-offsite /abs/path/<name>.dump.enc`
(`--offsite-root PREFIX` publishes under another prefix, for a scratch
check). Only encrypted artifacts reach the bucket, so its access model does
not expose database contents. Nothing here deletes a remote object; remote expiry
belongs to the bucket's lifecycle policy.

## Migration rollback snapshots

Tables named `*_backfill_*` are finite migration recovery snapshots, not
durable application state. The migration lint requires a later forward
migration with `DROP TABLE IF EXISTS` for every such table. Before that forward
retirement may run against a populated table, keep its recovery data by hand:
dump the one table with `pg_dump --format=custom --table=public.<table>` through
the owner dial the scheduled dump uses (`services.backup.dump_source()`) into
`$AVA_HOME/backups/<table>.dump` (mode 0600), confirm it with
`pg_restore --list` (the listing must name `TABLE DATA public <table>`), and
keep the file until the migration is verified in production. The dump is not
encrypted and is not published off-site; delete it once the snapshot has no
further use.
