---
type: doc
title: Database write-generation authority
description: Stable NOLOGIN capability groups, the home's one write generation, the private ledger and the fail-closed catalog invariant.
tags: [postgres, authority, lifecycle]
---

# Database write-generation authority

`base.cluster.authority` separates write authority from the schema. The
schema owner and two stable `NOLOGIN` groups hold every privilege; application
processes log in only as the home's one **write generation**: `ava_g0_gateway`
and `ava_g0_runner`, minted by the home's first start. Nothing revokes or
replaces it: a release does not change it (the
[code version gate](../../../db/docs/code-version-gate.ava.okf.md) keeps old code
from writing) and no operation rotates it. The decisions are
[internal data plane always authenticated](../../../../decisions/2026-09-26-internal-data-plane-always-authenticated.md)
and [retiring write-generation rotation](../../../../decisions/2026-10-03-retire-write-generation-rotation.md).

The package is a library. Every catalog function takes the caller's admin
connection: the OS-user superuser over the owner-only socket, autocommit,
without `SET ROLE`. Custody of that connection belongs to the opener.

## Roles

| Role | Shape |
|---|---|
| owner | `NOLOGIN`, no password, not a superuser, owns the database and schema objects, no memberships |
| `ava_gateway` | `NOLOGIN` group: DML on every table (SELECT/INSERT only on the append-only `audit_events` and `telemetry_events`), `USAGE, SELECT, UPDATE` on sequences, `EXECUTE` on every routine, `MAINTAIN` on the checkpoint tables; no TRUNCATE/REFERENCES/TRIGGER |
| `ava_runner` | `NOLOGIN` group: the audited runner matrix (the historical login, demoted in place) |
| `ava_g0_<class>` | `LOGIN NOSUPERUSER INHERIT NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS`, stored SCRAM verifier, owns nothing, no direct grant or setting, member of exactly its group with `INHERIT TRUE, SET FALSE, ADMIN FALSE` |
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
`ledger.json` records owner, groups and the generation as `pending` or
`active`. `generations/0.json` holds the two passwords, their client-computed
verifiers and one machine API token per class; its SHA-256 is the ledger's
`credential_digest`, the only credential fact other records may carry. Files are
0600, written atomically with file and directory fsync; reads refuse symlinks,
loose modes and foreign owners.

A ledger written before generations stopped rotating also carries
`counter: 0`, `revoked: []` and null `operation`/`direction` in the origin. They
are read and not written back; a ledger that records a rotation (a counter above
0, a revoked entry) refuses as corrupt. The origin `cutover` marks the
generation of a home converted before births minted the ledger; nothing mints
it now.

| Step | Durable effect |
|---|---|
| `create_ledger` | the empty ledger; an identical one is kept |
| `begin_mint` | secret published exclusively (link, never overwrite), then `pending`; a published secret without `pending` is adopted, never regenerated |
| `mint_generation` | refuses colliding names and foreign group members; creates only a missing role of the pending pair; existing roles must match exactly |
| `activate` | the exact verified pending pair becomes `active` (idempotent), only after the pooler serves it and both logins answer |

An interrupted birth retries to the same generation with the credentials it
already published; nothing is accepted or delivered before `activate`.

## Invariant

`invariant.check_invariant` is read-only and fail closed. It reports every
violation together: owner or group shape, members other than the generation's
pair, schema-owner membership, an auxiliary login that is a superuser, owns objects or can write
`public`, a monitoring login that differs from its shape, an ACL or
default-privilege grantee outside owner/groups/PUBLIC (`EXECUTE`, `USAGE`,
`TEMPORARY`), the monitor (`CONNECT` only) or an allowlisted read-only role, and
PUBLIC `CONNECT`. A missing monitor is not a violation; start creates it.

A group member outside the generation is a refusal, never a repair: start does
not demote or drop it.

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
against a home-owned PostgreSQL, PgBouncer and Redis (including the collector's monitoring dial), and
`test_delivery.py` covers delivery and the boot pass without a database;
`test_api_tokens.py` is the API token acceptance matrix (gateway, bootstrap,
login, webhooks, `/ops`, launch delivery, clients);
`test_unit_capability.py` covers the remote-unit bundle (sealing, binding,
install, the runner's boot pass and launcher) and, on the real gateway plane,
issue -> runner start -> generation login and the credential-free bootstrap. It
uses a throwaway instance with peer-only admin and SCRAM for every other role,
plus a template built by the superuser acting as the owner. Crash injection
covers each durable boundary of the birth. A mutation of every guard turns at
least one test red.
