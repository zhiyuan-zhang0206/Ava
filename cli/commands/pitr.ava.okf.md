---
type: doc
title: PITR CLI Commands
description: Inspect PITR retention evidence and archive finite migration rollback snapshots before their forward retirement.
tags:
- cli
- pitr
---

# PITR CLI Commands

`cli/commands/pitr.py` backs the `ava pitr` command group. `retention inspect`
renders the newest local retention dry-run plan without a delete surface.

`drill --chain <chain> --target-lsn <LSN> --target-wall <TS> --scratch <DIR>`
restores one protected physical chain to an operator-chosen target LSN in an
isolated sandbox, reports the acceptance criteria, publishes nothing and keeps
the scratch tree as evidence. See
`.agents/skills/operating-ava-cluster/references/physical-restore-drill.md`
for the operator workflow.

`snapshot archive <table>` accepts only the shared `*_backfill_*` rollback
snapshot convention. It exports the table, AES-GCM encrypts the custom dump,
and publishes it through the configured PITR store. Its owner-only local record
stores the immutable remote object identity: backend pin, checksum, and metadata.

`snapshot verify <table>` re-downloads that exact pinned object, authenticates
and decrypts it, and restores it into throwaway PostgreSQL before marking the
record verified. `snapshot retire <table>` refuses without that verified record,
then issues an idempotent direct `DROP TABLE IF EXISTS`. The migration lint uses
the same naming convention to require a later forward drop plan for every
rollback snapshot.

See `.agents/skills/operating-ava-cluster/references/db-restore.md` for the
operator workflow.
