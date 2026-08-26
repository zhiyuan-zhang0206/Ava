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

## Recommended automated drill

From the checkout that owns the backup cluster, run either command:

```bash
.venv/bin/python scripts/restore_drill.py
.venv/bin/python scripts/restore_drill.py /absolute/path/to/<db>-<utc>.dump.gz.enc
```

The first command selects the newest managed local artifact. The script creates
a native throwaway Postgres cluster, decrypts the artifact, unzips the custom
dump, restores with `pg_restore --clean --if-exists`, and removes all scratch
data and the cluster when it exits.

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
openssl enc -d -aes-256-cbc -pbkdf2 -salt -kfile "$key_file" -in /absolute/path/to/<db>-<utc>.dump.gz.enc -out "$scratch_dir/backup.dump.gz"
chmod 600 "$scratch_dir/backup.dump.gz"
gzip --decompress --stdout "$scratch_dir/backup.dump.gz" > "$scratch_dir/backup.dump"
chmod 600 "$scratch_dir/backup.dump"
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
retired. The gzip CRC and `pg_restore` failure path detect corruption; the
artifact is encrypted with AES-256-CBC and inherits the local artifact's 0600
threat model.

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

The gateway checks the existing Google Drive sync folder for a writable target.
After local encryption succeeds and before local pruning, it copies the
`.dump.gz.enc` artifact into `Ava Backups/<cluster-home-digest>/`, verifies its
byte count, and retains the newest seven daily artifacts plus the newest
pre-update snapshot there. The Drive copy
is optional: an unavailable sync folder emits a warning but never discards the
local artifact. Because only encrypted artifacts reach Drive, its access model
does not expose database contents. An object store can replace this copy stage
on hosts without a usable Drive sync folder.
