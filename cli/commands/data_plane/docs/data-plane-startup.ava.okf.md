---
type: doc
title: Gateway data-plane startup
description: How `ava start` brings up a home's own PostgreSQL, PgBouncer and Redis with URL-derived identities, always-authenticated pg_hba, the write generation and native custody checks.
tags:
- cli
- postgres
---

# Gateway data-plane startup

`ava start` on a gateway-capable unit brings up the home's own PostgreSQL,
PgBouncer and Redis before any application service.

## Identity

`cluster_instance` takes separate URL identities: the Postgres database/owner
comes from `db_identity()` (the URL's database), the Redis ACL user from
`redis_identity()`. First-start identity persists the Redis credentials before
effects, and the identity owner writes each URL before storage comes up; no
runtime identity backfill exists. Startup refuses missing Redis credentials,
and a home without a database authority ledger that is not mid-birth, before
any native effect (no conversion exists); it never substitutes the bearer. Explicit remote-managed URLs retain provider authority.

## Authentication and the write generation

`_pg_hba_body` always authenticates (the OS user by `peer` on the owner-only
socket, also as the password-less monitoring role `ava_monitor` through the
`pg_ident` map `_pg_ident_body` writes; every other role SCRAM) and
`require_authenticated_hba` proves the running postmaster demands passwords
after every rewrite. `bringup` wires the write generation
([[base/cluster/authority/docs/wiring.ava.okf.md|delivery and wiring]]): birth runs
groups -> monitor -> retire legacy logins -> ledger -> mint generation 0 ->
pooler serving that pair -> pooled proof of both logins -> activate; an
ordinary start re-grants the groups after migrations, converges the monitor
and holds on any catalog/ledger mismatch
before the pooler. `db_delivery` gives
each launched service its class login.

## WAL archiving launch arguments

When `AVA_WALG_CONFIG_FILE` is set, `_start_pg` appends `archive_mode`,
`archive_timeout` and `archive_command` (`services.backup.walg.archive.archive_pg_args`)
to the postmaster's `-c` list; unset adds nothing. The settings live only in the
launch arguments, and a retained postmaster is reloaded, not relaunched, so they
take effect at the next new launch. After Postgres is ready, `warn_archive_inactive`
prints a warning when the running Postgres reports other archive settings
([[services/backup/walg/docs/walg.ava.okf.md|WAL-G]]).

## Native custody

`base.cluster.ownership` is the common startup/maintenance observer: home
paths, native process birth and all listener PIDs must agree before config,
reload or ACL effects. Redis ACL and maintenance shutdown each retain one
observed connection; a reconnect loses authority and fails. PostgreSQL admin
and pooler dials use only the home's canonical Unix socket directory. Owned
provisioning, checkpoint, grant and migration dials verify their native
backend against the home's postmaster before DDL, which acts as the schema
owner (`base.db.pg_admin`).

The layout and bind facts the cli and the root diagnostics must agree on live in
`base.cluster`, not in the cli: the Redis data directory and port
(`ownership.redis_data_dir`, `ownership.configured_redis_port`), the data plane's
bind posture (`port_preflight.bind_addrs`: loopback alone without a cluster secret,
loopback plus the reachable address with one) and the pooler's files and listener
probes (`dataplane.pooler`). The cli owns bring-up and stop; the diagnostics only look.

## Pooler

PgBouncer starts only after schema and group grants exist, always with SCRAM
against the generation's verifier userlist; changed userlist or ini bytes
restart it (a reload never revokes a user). A live pooler with closed listeners
retains custody: normal start cannot repeat its shutdown signal or escalate to
force. A graceful stop timeout fails without killing the survivor.

Native maintenance-stop tests wait for both listener readiness and the daemon's
PID publication: an open port does not prove the PID file is complete. Their
bounded fixture wait accepts missing or empty publication while startup proceeds;
nonempty corrupt content fails immediately. The real exit and idle-client stop
contracts exercise a pending PID read and retain verified native cleanup custody.

PgBouncer and Redis are spawned with `base.native_process.child_env.daemon_process_env`,
and the Postgres postmaster with `base.cluster.dataplane.pg_tools.pg_start_env` (the same set
plus the macOS locale fallback): the operator's PATH, home, user, temp dir,
timezone and locale only. The gateway login, write generation and API token
the boot pass delivered to `ava start`, and the human secret and Redis admin
password `.env` gives it, never reach a data-plane daemon's environment.
Nothing the postmaster runs needs more.

Startup ordering and remote stop/status contracts are tested beside the data-plane
owner in `../tests/test_start_data_plane.py` and `../tests/test_remote_data_plane.py`.
The lifecycle tests retain the `ava start` caller dispatch contracts.
