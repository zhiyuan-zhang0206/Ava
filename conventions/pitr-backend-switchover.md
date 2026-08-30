# PITR backend switchover — GCS to Baidu Netdisk

Cut-over procedure for `AVA_PITR_STORE_BACKEND=baidu`. The GCS bucket stays the
retained primary copy: read-only for two weeks after a successful switch (hard
rule), then retention may age it out.

## Hard gates (all three, in order)

1. Restore drill PASS — `prove_candidate` end to end for the newest protected
   chain on the current backend (GCS) before the window, and again on Baidu
   after migration (the acceptance evidence).
2. User approves the cut-over time window (timing is a user decision).
3. Migration script completes with a clean report — any record referencing an
   unmigrated object aborts before a single record is rewritten.

## Executors

Ops agent (#1818) executes the procedure; #405 observes each gate.

## Procedure

### 0. Preconditions

- `pitr-uploader` + `pitr-base-candidate` services healthy (`/healthz` on
  :8117 / :8118).
- No in-flight activation: `ava cluster pitr status` shows a terminal phase
  (`protected` / `rolled_back`). The migration script refuses anything else.
- Baidu app credentials + token files in place at the configured
  `AVA_PITR_BAIDU_CREDENTIALS_FILE` / `AVA_PITR_BAIDU_TOKEN_FILE` paths.

### 1. Restore drill on GCS (pre-cut proof)

Run the existing drill for the newest protected chain; keep the evidence.

### 2. Stop the PITR daemons

Stop `pitr-uploader` and `pitr-base-candidate` so no ACK or candidate is
written while records are being rewritten.

### 3. Snapshot + migrate

```
python scripts/pitr_migrate_gcs_to_baidu.py \
    --gcs-project <project> --gcs-bucket <bucket> --gcs-prefix ava-pitr \
    --gcs-credentials <viewer-credentials>.json \
    --baidu-app-root <app-root> \
    --baidu-credentials <baidu-credentials>.json --baidu-token <baidu-token>.json \
    --records-root ~/.ava/physical-backup
```

The script snapshots the identity records (tarball next to the records),
streams every GCS object into the Baidu three-phase engine (sidecar included)
with md5 verified on both ends, writes the generation->fs_id:md5 mapping, and
rewrites ACK / candidate / protected records field-level. The GCS bucket is
never mutated.

**Pin the FIRST snapshot tarball before starting.** The script refuses to
overwrite an existing snapshot, so re-running it writes another tarball;
rolling back with a later one can restore half-rewritten records. Record
the first tarball path and use exactly that one for any rollback.

### Interrupted runs

- Interrupted during the copy phase: safe to re-run — identical objects
  are adopted via rapid transfer and the mapping converges.
- Interrupted during the record-rewrite phase: do NOT continue. Restore
  the FIRST snapshot tarball (see Rollback below), then re-run the
  script from scratch. A half-rewritten record set must never reach the
  config flip.

### 4. Config flip

Set `AVA_PITR_STORE_BACKEND=baidu` plus the three Baidu fields through the
settings path (never edit `.env` manually), then restart the gateway unit.

### 5. Post-cut smoke (acceptance evidence)

- WAL upload: force a segment (`pg_switch_wal`), watch the uploader ACK it to
  Baidu.
- One base-candidate cycle.
- Restore drill on Baidu (mandatory): `prove_candidate` for the migrated
  newest chain and the fresh chain.
- Throughput baseline: `scripts/pitr_baidu_speedtest.py`.

### 6. GCS read-only retention

Keep the GCS bucket read-only for two weeks. Do not delete objects or rotate
the uploader credentials away inside this window.

## Rollback (within the two-week window)

1. Stop the PITR daemons.
2. Restore the FIRST snapshot tarball over `~/.ava/physical-backup/`
   (the one pinned in §3 — not a later re-run's tarball).
3. Point `AVA_PITR_STORE_BACKEND` back at `gcs`; restart.
4. Verify with the GCS restore drill.

Baidu objects are left in place (harmless; retention may clean them later).

## Not covered

- Copying Baidu-written objects back into GCS (GCS keeps its copy).
- Deleting Baidu objects after the retention window — future retention work.
