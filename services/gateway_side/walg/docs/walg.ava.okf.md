---
type: doc
title: WAL-G — physical backup, WAL archiving base
description: One config key (AVA_WALG_CONFIG_FILE) turns on WAL archiving to Aliyun OSS through a pinned WAL-G binary; Postgres is launched with the archive settings as -c arguments, converge validates the configuration and pins the encryption key's fingerprint, and the health probe reports a broken archive in four fixed-text state conditions.
tags: []
---

# WAL-G — physical backup, WAL archiving base

## What is it
`services/gateway_side/walg/` is the gateway-side half of the physical backup:
Postgres ships every completed WAL segment, encrypted, to an Aliyun OSS prefix
through [WAL-G](https://github.com/wal-g/wal-g). It is off unless
`AVA_WALG_CONFIG_FILE` is set; unset, no part of it runs (no `-c` argument, no
download, no probe query). The daily logical dump
([[backup.ava.okf.md|PG-Backup]]) is a separate recovery path and shares no code,
lock, credential or bucket prefix with it.

This node covers the archiving base. Base backups, retention, restore and the
recovery drill are not part of it: archived WAL alone is not a recovery path.

## Core Responsibilities
- **One key, WAL-G's own format** (`base/config/walg.py`, domain `walg`):
  `AVA_WALG_CONFIG_FILE` names a 0600 JSON file holding `WALG_OSS_PREFIX`,
  `OSS_ACCESS_KEY_ID`, `OSS_ACCESS_KEY_SECRET`, `OSS_ENDPOINT`, `OSS_REGION`,
  `WALG_LIBSODIUM_KEY_PATH`, `WALG_LIBSODIUM_KEY_TRANSFORM=hex` and
  `WALG_PREVENT_WAL_OVERWRITE=true`. Ava does not translate it, so no credential is
  copied into `.env`. The key is host-scoped, writable (`ava config unset` always
  removes it) and never sent to other machines.
- **Validation without echo** (`config.py`): required keys, a bucket prefix that
  names a path and cannot sit under `ava-logical/`, `ava-pitr-scratch/` or
  `ava-wsl-cutover-*`, owner-only files, a 32-byte lowercase-hex key. Errors name
  keys and paths, never values.
- **Key fingerprint pin** (`config.py`): the first load writes the key's truncated
  SHA-256 to `$AVA_HOME/backups/walg/key-id` (0600); a later key file that differs
  is refused by converge and alerted by the probe. The key is the single point of
  loss: without it every archived segment is unreadable, so escrow it outside the
  host before enabling. No rotation: a new key means a new bucket prefix.
- **Pinned binary** (`base/cluster/dataplane/walg_binary.py`): a SHA-256-pinned
  release asset, run with `--version` before it replaces anything, at the
  version-free path `$AVA_HOME/runtime/walg/wal-g`. Linux x86_64 only; any other
  platform fails at converge when the key is set.
- **Launch arguments, not `ALTER SYSTEM`** (`archive.py`): `archive_mode=on`,
  `archive_timeout=60s` and `archive_command='<wal-g> --config <file> wal-push %p'`
  (with `%` in paths escaped) are appended to the postmaster's `-c` list by
  `cli/commands/data_plane/cluster_instance.py:_start_pg`. Nothing is written to the
  data directory, so a base backup carries no archive setting and switching off
  leaves nothing behind. `archive_mode` is read only at Postgres launch: a retained
  postmaster keeps its previous arguments, so enabling or disabling needs
  `ava stop` + `ava start`; start warns when the running Postgres differs.
- **One runner** (`runner.py`): `wal-g --config <file> ...` with the daemon
  environment subset (no `AVA_*` value, no storage credential) and bounded time.
- **Pre-flight** (`check.py`, `ava backup walg check`): binary, configuration, key,
  then one put / list / get / delete / list round trip under the prefix. The delete
  matters: retention would be the first delete, weeks after setup.
- **Health** (`probe.py`, called by `ava health-probe`): four states, each with a
  fixed text so an outage stays one alert episode. (1) The running Postgres carries
  the configured archive settings. (2) The archiver is not failing
  (`last_failed_time` newer than `last_archived_time`). (3) No complete WAL segment
  has waited in `archive_status/` (the `.ready` marker's age) longer than the
  300 s RPO objective; a merely idle database has nothing queued, and a hung archive
  command, which neither succeeds nor fails, is caught here. (4) The key file is the
  pinned key. Listing the markers needs superuser or `pg_monitor`, which the
  application login lacks, so the probe reads over the owner-only admin socket; it
  writes nothing and never raises.

## Key Dependencies
- [[backup.ava.okf.md|PG-Backup]] — the independent logical path; its off-site namespace root is excluded from the WAL-G prefix
- [[data-plane-startup.ava.okf.md|Data plane startup]] — `_start_pg` passes the launch arguments

## Entry Points
- `cli/commands/converge/walg.py` — the converge step: install, validate, pin, before Postgres starts
- `cli/commands/data_plane/walg.py` — `ava backup walg check|status` and the start-time warning
- `services/gateway_side/walg/archive.py` — `archive_pg_args()`, `expected_archive()`, `ARCHIVE_TIMEOUT_S`, `RPO_OBJECTIVE_S`
- `cli/commands/cluster/health.py` — the probe hook, a check beside disk usage

## Notes
- Operator procedure (enable, disable, lifecycle rule, escrow): `conventions/runbook.md`, "WAL-G archiving". Why WAL-G and why launch arguments: [decision](../../../../decisions/2026-10-02-walg-physical-backup.md).
- Gateway capability only; a remote-managed data plane is refused at converge.
