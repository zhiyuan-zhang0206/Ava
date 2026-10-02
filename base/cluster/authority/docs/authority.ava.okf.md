---
type: doc
title: Database write-generation authority
description: Stable NOLOGIN capability groups, per-generation logins, the private ledger, the census-proven fence and the fail-closed catalog invariant.
tags: [postgres, authority, lifecycle]
---

# Database write-generation authority

`base.cluster.authority` separates write authority from the schema. The
schema owner and two stable `NOLOGIN` groups hold every privilege; application
processes log in only as one **write generation**: `ava_g<n>_gateway` and
`ava_g<n>_runner`. A rotation (the runbook's manual procedure after a credential
leak) revokes the old generation, proves its sessions closed, and mints the
next; a revoked number never logs in again. The decision is
[internal data plane always authenticated](../../../../decisions/2026-09-26-internal-data-plane-always-authenticated.md).

The package is a library. Every catalog function takes the caller's admin
connection: the OS-user superuser over the owner-only socket, autocommit,
without `SET ROLE`. Custody of that connection belongs to the opener.

## Roles

| Role | Shape |
|---|---|
| owner | `NOLOGIN`, no password, not a superuser, owns the database and schema objects, no memberships |
| `ava_gateway` | `NOLOGIN` group: DML on every table (SELECT/INSERT only on the append-only `audit_events` and `telemetry_events`), `USAGE, SELECT, UPDATE` on sequences, `EXECUTE` on every routine, `MAINTAIN` on the checkpoint tables; no TRUNCATE/REFERENCES/TRIGGER |
| `ava_runner` | `NOLOGIN` group: the audited runner matrix (the historical login, demoted in place) |
| `ava_g<n>_<class>` | `LOGIN NOSUPERUSER INHERIT NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS`, stored SCRAM verifier, owns nothing, no direct grant or setting, member of exactly its group with `INHERIT TRUE, SET FALSE, ADMIN FALSE` |
| `ava_monitor` | stable, not a generation: `LOGIN INHERIT`, nothing elevated, **no password** (pg_hba admits it only by `peer` from the home's OS user over the owner-only socket), member of exactly `pg_read_all_stats` (`INHERIT TRUE, SET FALSE`), only direct grant `CONNECT` on the cluster database, owns nothing; the OTel collector's PostgreSQL receiver (`monitor.ensure_monitor`) |

Both groups hold `CONNECT` (PUBLIC loses it) and `USAGE` on `public`.
`ALTER DEFAULT PRIVILEGES FOR ROLE <owner>` covers objects later migrations
create; `groups.ensure_groups` re-runs the point-in-time `ALL` grants after a
migration. A login's privileges equal its group's. The runner group also holds
`EXECUTE` on the retired publication-admission lock function while it exists;
no admission calls it, and a later migration drops it.

`groups.vacuum_or_fail` turns PostgreSQL 17's VACUUM skip warning (missing
`MAINTAIN`) into a failure.

## Ledger

`$AVA_HOME/db-authority/` (0700) is outside the configuration digest.
`ledger.json` records owner, groups, `counter`, `active`, `pending` and every
revoked number. `generations/<n>.json` holds the two passwords, their
client-computed verifiers and one machine API token per class; its SHA-256 is
the ledger's `credential_digest`, the only credential fact other records may
carry. Files are 0600, written
atomically with file and directory fsync; reads refuse symlinks, loose modes
and foreign owners. Every number `0..counter` is exactly one of active,
pending or revoked, so `counter` never decreases.

Mutations take a typed token: `BirthAuthority` mints only generation 0;
`OperationAuthority(operation, direction)` mints later numbers, revokes, closes
and records drops. Birth runs under the start intent's lock; every ledger
mutation takes the ledger lock itself. A recorded origin may also read `cutover`:
generation 0 of a home converted before births minted the ledger, which nothing
mints now.

| Step | Durable effect |
|---|---|
| `begin_mint` | secret published exclusively (link, never overwrite), then `pending`; a secret for the next number without `pending` is adopted |
| `mint_generation` | refuses colliding names and foreign group members; creates only a missing role of the pending pair; existing roles must match exactly |
| `activate` | the exact verified pending pair becomes `active` (idempotent) |
| `revoke` | unrevoked generation becomes `revoked[revoking]`, then the sweep |
| `close_revoked` | closure proof, `revoked[closed]`, secret deleted |
| `prune` | `DROP ROLE` per login; a dependency leaves a `NOLOGIN` tombstone and its exact error; never `DROP OWNED` or `CASCADE` |

A retry at any boundary continues the same generation or holds. A different
authority can neither adopt nor activate another's pending generation; a
pending generation whose roles were never created can still be revoked and
closed.

## Sweep and fence

`sweep` is monotone. In one transaction it removes LOGIN and the password from
every recorded non-active generation login, every other transitive group
member, the owner and the groups, then revokes those roles' group memberships
by recorded grantor. No function sets LOGIN on an existing role.

`prove_closure` requires every named role to be `NOLOGIN` and a member of
nothing. It terminates survivors with `pg_terminate_backend(pid, timeout)` and
re-censuses for bounded rounds. A `true` return is only a sent signal; only an
empty census closes. The census covers the named roles, every session whose
role can no longer log in, and sessions of dropped roles, because PostgreSQL
lets `DROP ROLE` succeed under a live session. Any prepared transaction holds.
A generation backend that authenticated just before its role lost LOGIN may
appear after the census; its only capability, membership, was revoked in the
same transaction. The caller stops the owned pooler first.

## Invariant

`invariant.check_invariant` is read-only and fail closed. It reports every
violation together: owner or group shape, members other than the unrevoked
pair, schema-owner membership, a revoked login that can log in or holds
membership, an auxiliary login that is a superuser, owns objects or can write
`public`, a monitoring login that differs from its shape, an ACL or
default-privilege grantee outside owner/groups/PUBLIC (`EXECUTE`, `USAGE`,
`TEMPORARY`), the monitor (`CONNECT` only) or an allowlisted read-only role, and
PUBLIC `CONNECT`. A missing monitor is not a violation; start creates it.

The sweep and the closure census never touch the monitor: it is not a group
member and can log in, so a rotation neither demotes it nor terminates its
sessions.

## Delivery and wiring

How the active generation reaches the pooler, launched services, operator
processes and remote agent-runner units (`unit`: a sealed bundle issued per unit,
whose login and tokens every runner unit shares), and
where birth and ordinary start call this library (API tokens:
[[base/cluster/authority/docs/api-tokens.ava.okf.md|machine API tokens]]):
[[base/cluster/authority/docs/wiring.ava.okf.md|Write-generation delivery and wiring]].

## Tests

`tests/lifecycle/db_authority/` runs on real PostgreSQL 17 through
`authority_postgres`; `test_single_box.py` drives the real start steps
against a home-owned PostgreSQL, PgBouncer and Redis (including the collector's monitoring dial across a rollover), and
`test_delivery.py` covers delivery and the boot pass without a database;
`test_api_tokens.py` is the API token acceptance matrix (gateway, bootstrap,
login, webhooks, `/ops`, launch delivery, clients);
`test_unit_capability.py` covers the remote-unit bundle (sealing, binding,
install, the runner's boot pass and launcher) and, on the real gateway plane,
issue -> runner start -> generation login, the credential-free bootstrap and a
revoked generation's bundle. It
uses a throwaway instance with peer-only admin and SCRAM for every other role,
plus a template built by the superuser acting as the owner. Crash injection
covers each durable boundary. A mutation of every guard turns at least one test
red.
