# Delete the self-written PITR stack; WAL-G is the replacement

## Context

`services/pitr/` was a self-written physical backup system: an `archive_command`
shim and a spool, an uploader daemon, weekly `pg_basebackup` candidates in a
private encrypted stream format, generation-pinned restore proofs, a retention
planner and delete executor, and four object-store adapters (GCS, Baidu
Netdisk, Tencent COS, Aliyun OSS) behind eight roles each. With its CLI, its 33
configuration keys and their cc-80 validator, its tests and its documents it was
about 35,000 lines.

It never gave this cluster a recovery point. Its activation was operator-owned
and was not completed: the physical chain stopped being written on 2026-09-19,
`archive_mode` is off, and the only recovery media is the daily encrypted logical
dump. Its restore path is code this repository has to keep working, because no
other tool can read its objects. Meanwhile a scratch drill of WAL-G against the
same object store restored a full-plus-incremental chain to a chosen time,
replayed the whole WAL history from the oldest full backup, and survived a
retention delete, each result identical to the source database. The drill's
limit was upload bandwidth, not the tool.

## Decision

Delete the stack and everything that only served it, and build point-in-time
recovery on WAL-G as separate work:

- `services/pitr/**`, `ava pitr ...`, the two roster entries and their flag
  gates, the converge step that published the archive shim, the `replication`
  rows `pg_hba.conf` carried for the PITR role, the one-shot GCS-to-Baidu
  migration and speed-test scripts, and every test of them.
- The physical-backup settings domain (33 keys). The daily dump's off-site
  destination, the one live part, is now `AVA_BACKUP_OFFSITE_ENDPOINT`,
  `AVA_BACKUP_OFFSITE_BUCKET` and `AVA_BACKUP_OFFSITE_CREDENTIALS_FILE` in the
  services domain, beside `AVA_BACKUP_HOUR` and `AVA_BACKUP_KEEP`; the backend
  switch is gone because OSS is the only backend.
- The `pitr_remote_inventory` event and its storage-growth alert, the two health
  port slots and pidfile settings, the `google-cloud-storage` and `zstandard`
  direct dependencies, and the base helpers only the stack used
  (`strict_decode`, `read_health_payload`, `init_restricted_process`,
  `restricted_process_env`, `forwarded_proxy_env`, `replace_env_bytes_cas`).
- The rollback-snapshot archive (`ava pitr snapshot`): a `*_backfill_*` table is
  dumped by hand with `pg_dump --table` before its retirement.

The last commit on `main` that still contains the stack is the tag
`pitr-stack-final`.

## Alternatives rejected

- **Keep the stack dormant behind its disabled-by-default flags.** It still
  costs two roster entries, a converge step on every start, a validator that runs
  at every settings build, a Google SDK, and about 32,000 lines of code and tests to
  read and keep green, for a result the tested alternative gives with far less of our code.
- **Finish activating it.** Its design is the part WAL-G already is: spool,
  uploader, base chain, retention. A recovery would depend on this repository's
  own reader of its own format, which is exactly the code that cannot be tested
  against anything but itself.
- **Move it to a plugin or another repository.** No consumer exists.
- **Keep the Baidu, COS and GCS adapters.** The migration they served is over and
  only OSS is in use.
- **Keep `ava pitr snapshot`.** It needed the stack's encryption key and store
  roles to preserve a table the migration lint already requires to be retired.
- **Rename the off-site keys later.** The keys are official config on both sides
  of the rename (writable, cluster-pinned or host), so the rename is an
  unset-before, set-after step on `ava config`, and keeping a PITR prefix on the
  one live key would be the leftover that outlives the stack.

## Consequences

- There is no point-in-time recovery until the WAL-G integration lands. The
  recovery point is the newest published daily dump; the disaster-recovery matrix
  says so.
- The physical chain already in object storage has no reader in this repository
  any more. The tag keeps the code that wrote it, and the key file its
  `AVA_PITR_BACKUP_KEY_FILE` named must not be deleted while anyone may still need
  those objects.
- The rollout has three manual steps that the upgrade cannot do itself (reset any
  archive settings a home carries, unset the writable retired keys while the old
  code still declares them and move the off-site destination to its new keys,
  delete the two retired slots from each gateway home's start intent); they are
  in the runbook.
- 24 retired keys stay in a home's `.env`, inert: settings ignore a key they do
  not declare, and official config can no longer unset a key it does not know.
- The statement in `2026-09-30-remove-release-image-path.md` that a home whose
  activation completed keeps its archive settings and its backup, upload,
  retention and drill services no longer holds.
