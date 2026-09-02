# Encrypted database restore drill

Use this procedure to prove that a managed Postgres backup can recover an Ava
cluster. The artifact contains the entire database, including LangGraph
checkpoint tables: those tables are the only copy of conversation history.
Never restore an artifact into the live database.

## Recovery objectives

- **RPO:** one day at the daily cluster-time backup window (`BACKUP_HOUR = 03:00`).
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
.venv/bin/python scripts/restore_drill.py
.venv/bin/python scripts/restore_drill.py /absolute/path/to/<db>-<utc>.dump.enc
```

The first command selects the newest managed local artifact. The script creates
a native throwaway Postgres cluster, decrypts the artifact (decompressing a
legacy gzip layer when present), restores with `pg_restore --clean
--if-exists`, and removes all scratch data and the cluster when it exits.

A successful run prints this shape (counts vary by artifact):

```text
restore drill passed: agents=567 checkpoints=5677 checkpoint_blobs=7971 checkpoint_writes=5680 sample_agent=42 messages=18 elapsed_seconds=381.4
```

Expected facts:

- `agents`, `checkpoint_blobs`, `checkpoints`, and `checkpoint_writes` are all
  present in the restored schema and their counts are printed.
- `sample_agent` names a restored checkpoint thread; `messages` is read through
  `shared.checkpoint.load_checkpoint_messages_full`, not raw table bytes.
- The successful checkpoint-reader call is the service smoke: it proves the
  restored LangGraph schema and serialized conversation data are usable.

## Manual transform reference

The automatic script is preferred because it owns throwaway-Postgres cleanup.
For an operator investigating an artifact, the transform it performs is:

```bash
scratch_dir=$(mktemp -d)
chmod 700 "$scratch_dir"
key_file="$scratch_dir/backup.key"
.venv/bin/python -c 'import hashlib; from shared.config import settings; print(hashlib.sha256(settings.data_plane.cluster_secret.encode()).hexdigest())' > "$key_file"
chmod 600 "$key_file"
openssl enc -d -aes-256-cbc -pbkdf2 -salt -kfile "$key_file" -in /absolute/path/to/<db>-<utc>.dump.enc -out "$scratch_dir/backup.dump"
chmod 600 "$scratch_dir/backup.dump"
# Legacy artifacts (<db>-<utc>.dump.gz.enc) need one extra step:
# gzip --decompress --stdout "$scratch_dir/backup.dump" > "$scratch_dir/backup.dump.raw" && mv "$scratch_dir/backup.dump.raw" "$scratch_dir/backup.dump"
```

The key file is the SHA-256 hex digest of the cluster secret. It is private,
never passed on argv, and must be deleted with the scratch directory after the
drill. The cluster secret itself lives in the surviving unit's `.env`
(`$AVA_HOME/.env`, mode 0600) — in a disaster-recovery scenario the secret from
any surviving runner (or the gateway) is sufficient to decrypt every artifact,
because the passphrase is derived from the cluster secret alone, not from any
per-host value. Note: rotating the cluster secret makes artifacts encrypted
under the previous value unrecoverable — after any rotation, keep the prior
secret in escrow (or re-run a backup) until the old artifacts have been
retired. The archive's compression CRC and `pg_restore` failure path detect
corruption; the artifact is encrypted with AES-256-CBC and inherits the local
artifact's 0600 threat model.

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
the `.dump.enc` artifact through the shared backup store contract — the same
backend switch as the physical PITR plane (`AVA_PITR_STORE_BACKEND`), under
the `ava-logical/` namespace. The publish is if-absent and store-verified
(ACK: pin_token, size, checksum). It is optional: a missing or unconfigured
store, or a failed publish, emits a warning but never discards the local
artifact — the local copy remains the primary. Because only encrypted
artifacts reach the store, its access model does not expose database contents.
Remote objects are append-only (the store contract has no delete verb); remote
retention is a shared planner concern and a follow-up.

## Migration rollback-snapshot archive

Tables named `*_backfill_*` are finite migration recovery snapshots, not
durable application state. The migration lint requires a later forward
migration with `DROP TABLE IF EXISTS` for every such table. Before that forward
retirement may run against a populated table, preserve its recovery data with
the official three-step workflow from the gateway checkout:

```bash
.venv/bin/ava pitr snapshot archive <table>
.venv/bin/ava pitr snapshot verify <table>
.venv/bin/ava pitr snapshot retire <table>
```

`archive` creates a custom-format dump of the one table, encrypts it with the
configured PITR AES-GCM key, and publishes it under a content-addressed
rollback-snapshot object name through the configured offsite store. The local
owner-only evidence record at `$AVA_HOME/rollback-snapshot-archives/<table>.json`
contains the backend acknowledgement: object name, generation pin, checksum,
and metadata.

`verify` downloads exactly that recorded generation through the viewer path,
authenticates and decrypts it, restores it into a throwaway PostgreSQL cluster,
and reads the restored table. It then records successful verification in the
same local evidence record. `retire` refuses until that verification exists;
once it does, it performs the idempotent `DROP TABLE IF EXISTS` on the live
snapshot table. Do not delete or edit the evidence record between these steps:
the record is the guard that binds retirement to the archived, drilled object.
