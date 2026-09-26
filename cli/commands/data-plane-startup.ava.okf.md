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

`_cluster_instance` takes separate URL identities: the Postgres database/owner
comes from `db_identity()` (the URL's database), the Redis ACL user from
`redis_identity()`. First-start identity persists the Redis credentials before
effects, and the identity owner writes each URL before storage comes up; no
runtime identity backfill exists. Startup refuses missing Redis credentials,
and a home without a database authority ledger that is not mid-birth, before
any native effect (naming `scripts/cutover_db_authority.py`); it never
substitutes the bearer. Explicit remote-managed URLs retain provider authority.

## Authentication and the write generation

`_pg_hba_body` always authenticates (the OS user by `peer` on the owner-only
socket, every other role SCRAM) and `require_authenticated_hba` proves the
running postmaster demands passwords after every rewrite. `_data_plane` wires
the write generation
([[shared/cluster/authority/wiring.ava.okf.md|delivery and wiring]]): birth runs
groups -> retire legacy logins -> ledger -> mint generation 0 -> pooler serving
that pair -> pooled proof of both logins -> activate; an ordinary start
re-grants the groups after migrations, sweeps stale logins to `NOLOGIN` and
holds on any catalog/ledger mismatch before the pooler. `db_delivery` gives
each launched service its class login.

## Native custody

`shared.cluster.ownership` is the common startup/maintenance observer: home
paths, native process birth and all listener PIDs must agree before config,
reload or ACL effects. Redis ACL and maintenance shutdown each retain one
observed connection; a reconnect loses authority and fails. PostgreSQL admin
and pooler dials use only the home's canonical Unix socket directory. Owned
provisioning, checkpoint, grant and migration dials verify their native
backend against the home's postmaster before DDL, which acts as the schema
owner (`shared.pg_admin`).

## Pooler

PgBouncer starts only after schema and group grants exist, always with SCRAM
against the generation's verifier userlist; changed userlist or ini bytes
restart it (a reload never revokes a user). A live pooler with closed listeners
retains custody: normal start cannot repeat its shutdown signal or escalate to
force. A graceful stop timeout fails without killing the survivor.
