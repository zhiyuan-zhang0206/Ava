---
type: doc
title: Code version gate
description: The client-side gate that stops a process running older code than the cluster minimum from writing - the code version, the minimum on deployment_state, the check inside the pooled-session restore, the exit, and what is exempt.
tags: [postgres, lifecycle, deploy]
---

# Code version gate

A unit that misses an update (a runner offline while the script stops, checks out
and starts every unit) later wakes with its old processes still running. The gate
makes such a process stop itself: every pooled session compares the process's
**code version** with `deployment_state.min_code_version` and refuses to work under
a lower one. Why it is client-side, what it costs, and what it does not cover:
[decision](../../../docs/decisions/2026-09-30-client-side-code-version-gate.md).
Operator procedures (reading state, rollback, credential rotation):
[runbook](../../../docs/conventions/runbook.md#code-version-gate).

## Code version

`base/native_process/code_version.py` (standard library only): the number of
first-parent commits reachable from the commit the process loaded
(`git rev-list --count --first-parent <sha>`), from `loaded_commit`'s boot capture
and never from the checkout as it later became, cached for the process lifetime.
A tree that is not a git checkout raises `CodeVersionError`; nothing substitutes
`0`. A shallow clone under-counts, which only makes its processes look older.

## The minimum

`deployment_state.min_code_version` (`BIGINT NOT NULL DEFAULT 0`). Every gateway
start raises it once, in `gateway/cluster/server.py` after the schema assertion and the
logger init: `raise_min_code_version` runs `UPDATE ... SET min_code_version =
GREATEST(min_code_version, <version>) WHERE id = 1` and refuses when the singleton
row is missing. A runner's local gateway does not (`is_gateway()`), its login
cannot write the row. Nothing lowers the minimum but an operator.

## The check

`base/db/connections.py` runs the baseline-session restore on every pooled dial,
pool-backend creation and borrow (`_restore_pooled_session` and its async twin).
Its second statement re-applies the statement ceiling and the connection's
`application_name` that `RESET ALL` cleared; on a borrow where
`code_version_gate.min_read_due()` (the process's first, then at most every
`MIN_REFRESH_INTERVAL_S`) that same statement also selects the minimum, so the
read adds no round trip. `observe_minimum` then compares: a process below the
minimum logs `critical` and exits through `hard_exit` with
`CODE_BEHIND_MINIMUM_EXIT_CODE` (78). It never raises, because `psycopg_pool`
swallows an exception from a `check` or `configure` callback and retries to
`PoolTimeout`, and `configure` runs on a thread nothing reads. A cluster without a
`deployment_state` row reads as `0`.

`pool()`, `async_pool()` and pooled `connect()` also start every connection with
`application_name = ava:<process>:v<version>` (`code_version_gate.application_name`),
resolving the version at construction so a tree without git fails there, on the
caller's thread. PgBouncer's `SHOW CLIENTS` lists these names.

## Not gated

- Direct connections (`direct=True`: administrator, migration applier, `pg_dump`) and
  `connect_url` targets. They carry no name and read no minimum.
- The `ava` CLI: `cli/main.py` calls `code_version.exempt_from_db_gate()` first,
  because `ava stop` writes to drain agents and a stale host must still run it. It
  is the only caller (guarded by `tests/cli/test_main_dispatch_contract.py`), and
  it dials as `ava:cli` without resolving a version. Services start with
  `python -m <module>`, so all of them are gated.
- A process that predates the gate, and one that holds a single raw connection
  forever.

Tests: `base/native_process/tests/test_loaded_commit.py` (the count, on real repositories),
`base/db/tests/test_connect_helpers.py` (the read schedule, the verdict and exit,
the restore against a fake and a real Postgres, the raise, the migration pair),
`tests/cli/test_pgbouncer_wire.py` (the name in a real PgBouncer's client list).

Parent: [[base/docs/infrastructure-utilities.ava.okf.md|infrastructure utilities]].
