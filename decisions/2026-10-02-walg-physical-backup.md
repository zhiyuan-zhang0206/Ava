# Physical backup runs on WAL-G, switched on by one config key

## Context

The self-written PITR stack is gone
([decision](2026-10-02-delete-the-self-written-pitr-stack.md)), so the recovery
point below the daily logical dump is empty. A scratch drill of WAL-G against the
same object store restored a full-plus-incremental chain to a chosen time, replayed
the whole WAL history from the oldest full backup, survived a retention delete, and
showed what to watch: `wal-verify` exits 0 while reporting a gap, `backup-fetch` brings back
the source's `archive_mode=on`, and a lost encryption key makes every object unreadable.
Upload bandwidth (about 2.2 MiB/s) is the real limit, not the tool.

This record covers the base the rest builds on: how archiving is configured, started,
verified and watched.

## Decision

- **One key, WAL-G's own file.** `AVA_WALG_CONFIG_FILE` names a 0600 JSON in WAL-G's
  native format. Ava validates it and passes it through (`wal-g --config <file>`); it
  does not translate it into typed settings. Unset means every WAL-G path is a no-op.
  The key is host-scoped and writable so it can always be removed (the retired
  PITR settings were mostly read-only and could not be).
- **Archive settings are `postgres` launch arguments.** `archive_mode`,
  `archive_timeout=60s` and `archive_command` are `-c` arguments of the postmaster,
  like `listen_addresses`. Nothing is written to the data directory: a base backup
  carries no archive setting, a restored instance cannot archive into the live chain
  by accident, and switching off is "unset the key, restart". The cost is that
  `archive_mode` is read only at launch, and an update retains the postmaster: enabling
  or disabling is an explicit `ava stop` + `ava start`, start warns when the running
  Postgres differs, and the health probe reports it.
- **No wrapper script.** The archive command is the pinned binary, `--config` and
  `wal-push %p`; the secrets stay in the 0600 file WAL-G reads on every call, never in
  argv, `postgresql.conf` or the environment. `pg_stat_archiver` and the Postgres log
  already give every call's result and time.
- **A pinned binary at a version-free path.** A SHA-256-pinned release asset is run
  once before it replaces anything and installed at `$AVA_HOME/runtime/walg/wal-g`.
  An upgrade is an atomic file replacement that needs no Postgres restart. Only Linux
  x86_64 is pinned, because upstream builds only Linux; enabling anywhere else fails at
  converge.
- **The key is pinned on first use.** The first load records a truncated SHA-256 of the
  libsodium key; a different key file is refused and alerted. There is no rotation: a new key
  means a new bucket prefix (a new generation), because two keys in one chain make it
  unreadable. Escrow of the key is a precondition of enabling, done by the operator.
- **`WALG_PREVENT_WAL_OVERWRITE=true` is required** in the configuration, so a segment
  name written twice with different content (a restored instance, a reused prefix) fails
  instead of mixing two histories. The prefix must name a path and cannot sit under the
  logical-dump, scratch or retired-chain namespaces.
- **The probe judges states, not thresholds.** Four conditions with fixed texts (a number
  in a text would start a new alert episode at every run): the running archive settings
  match; the archiver is not failing; no complete segment has waited in `archive_status/`
  longer than the 300 s RPO objective; the key file is the pinned key. Waiting is the age
  of the oldest `.ready` marker, which is zero on an idle database and grows under a hung
  archive command that neither succeeds nor fails. Listing the markers needs superuser or
  `pg_monitor`, so the probe reads over the owner-only admin socket, read-only.
- **No back-pressure.** Nothing limits upload rate or blocks writes: a failing or slow
  archive shows as `pg_wal` growth and an alert, not as stalled transactions. The disk
  watermark check is the last line.

## Alternatives rejected

- **Typed Ava settings for prefix, endpoint, region and credential path.** A translation
  layer, two sources of truth for one credential, and a field to keep in step with every
  WAL-G option; the native file has none of that.
- **`ALTER SYSTEM` or `postgresql.conf` for the archive settings.** The setting would live
  in the data directory, travel inside base backups, and survive "off".
- **A wrapper script around `wal-push`.** It only served measurement and fault injection
  in the drill.
- **A time-since-last-archive threshold.** On an idle database no data is waiting, yet the
  age keeps growing and the alert fires; the queue's contents decide.
- **Reading the queue as the application login, or by segment arithmetic.** The login is
  refused the listing, and the arithmetic cannot tell an idle database from a stuck one when
  nothing has ever been archived.
- **Pinning the other release assets (aarch64, other Ubuntu versions).** Unproven builds;
  fail fast until one is needed and exercised.

## Consequences

- Enabling archiving is a whole-cluster stop and start, scheduled by the operator; the
  runbook says so and the key's precondition (config check, escrow) comes first.
- The archive is not a recovery path by itself: base backups, retention and restore are
  separate work, and until they exist the newest published daily dump stays the recovery
  point.
- A lost or replaced key is not recoverable by this code; the pin only makes the mistake
  loud before it is made.
- A hung `wal-g` also hangs Postgres' shutdown while segments are queued, because the
  archiver finishes its queue first; the runbook tells the operator to check the queue before
  a planned stop.
