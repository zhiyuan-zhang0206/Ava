---
type: doc
title: Database write-generation authority
description: Stable NOLOGIN capability groups, per-rollout generation logins, the private ledger, the census-proven fence and the fail-closed catalog invariant.
tags: [postgres, authority, lifecycle]
---

# Database write-generation authority

`shared.cluster.authority` separates write authority from the schema. The
schema owner and two stable `NOLOGIN` groups hold every privilege; application
processes log in only as one **write generation**: `ava_g<n>_gateway` and
`ava_g<n>_runner`. A rollout revokes the old generation, proves its sessions
closed, and mints the next. Returning to an earlier image mints a new number;
a revoked number never logs in again. The decision is
[internal data plane always authenticated](../../../decisions/2026-09-26-internal-data-plane-always-authenticated.md).

The package is a library. Every catalog function takes the caller's admin
connection: the OS-user superuser over the owner-only socket, autocommit,
without `SET ROLE`. Custody of that connection belongs to the opener.

## Roles

| Role | Shape |
|---|---|
| owner | `NOLOGIN`, no password, not a superuser, owns the database and schema objects, no memberships |
| `ava_gateway` | `NOLOGIN` group: DML on every table, `USAGE, SELECT, UPDATE` on sequences, `EXECUTE` on every routine, `MAINTAIN` on the checkpoint tables; no TRUNCATE/REFERENCES/TRIGGER |
| `ava_runner` | `NOLOGIN` group: the audited runner matrix (the historical login, demoted in place) |
| `ava_g<n>_<class>` | `LOGIN NOSUPERUSER INHERIT NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS`, stored SCRAM verifier, owns nothing, no direct grant or setting, member of exactly its group with `INHERIT TRUE, SET FALSE, ADMIN FALSE` |

Both groups hold `CONNECT` (PUBLIC loses it) and `USAGE` on `public`.
`ALTER DEFAULT PRIVILEGES FOR ROLE <owner>` covers objects later migrations
create; `groups.ensure_groups` re-runs the point-in-time `ALL` grants after a
migration. A login's privileges equal its group's. Until start wiring retires
`shared.cluster.provision.ensure_runner_role`, a real-PostgreSQL test holds
the two runner matrices equal except the publication-admission `EXECUTE` (not
carried forward) and the new `CONNECT`/`USAGE`.

`groups.vacuum_or_fail` turns PostgreSQL 17's VACUUM skip warning (missing
`MAINTAIN`) into a failure.

## Ledger

`$AVA_HOME/db-authority/` (0700) is outside the configuration digest.
`ledger.json` records owner, groups, `counter`, `active`, `pending` and every
revoked number. `generations/<n>.json` holds the two passwords and their
client-computed verifiers; its SHA-256 is the ledger's `credential_digest`,
the only credential fact other records may carry. Files are 0600, written
atomically with file and directory fsync; reads refuse symlinks, loose modes
and foreign owners. Every number `0..counter` is exactly one of active,
pending or revoked, so `counter` never decreases.

Mutations take a typed token: `BirthAuthority` and `CutoverAuthority` mint only
generation 0; `OperationAuthority(operation, direction)` mints later numbers,
revokes, closes and records drops. The caller holds the matching lock
(start intent, cutover, or the operation lock).

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
same transaction. A legacy owner racing the cutover is excluded by the
cutover's precondition (application root absent). The caller stops the owned
pooler first.

## Invariant

`invariant.check_invariant` is read-only and fail closed. It reports every
violation together: owner or group shape, members other than the unrevoked
pair, schema-owner membership, a revoked login that can log in or holds
membership, an auxiliary login that is a superuser, owns objects or can write
`public`, an ACL or default-privilege grantee outside owner/groups/PUBLIC
(`EXECUTE`, `USAGE`, `TEMPORARY`) or an allowlisted read-only role, and PUBLIC
`CONNECT`.

## Tests

`tests/lifecycle/db_authority/` runs on real PostgreSQL 17 through
`authority_postgres`. It uses a throwaway instance with peer-only admin and
SCRAM for every other role, plus a template built by the superuser acting as
the owner. Crash injection covers each durable boundary. A mutation of every
guard turns at least one test red.
