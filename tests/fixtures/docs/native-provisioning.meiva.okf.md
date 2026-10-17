---
type: doc
title: Native test resource isolation
description: Native test resource isolation.
tags: [contracts]
---

# Native test resource isolation

Each requested native resource has one session owner per pytest process;
xdist workers own separate sessions. Function fixtures install its URL in
settings, environment and the private .env, then restore them. Unrelated tests
keep unreachable sentinels and driver refusal. Static processes refuse native
resource requests but retain session safety hooks. Entry image resolution occurs
before tests can replace subprocess behavior. Gateway clients receive only the
test secret; authentication tests explicitly remove it.

The session-start guard refuses a non-test database before fixtures or tests can
run. The full-run guard resolves positional arguments against the invocation
directory as well as the repository and recognizes options that consume values.
The cleanup table list is AST-audited against schema tables: non-FK derived rows,
including timeline projections, must be listed. Singleton and infrastructure
exemptions belong to that audit. `cluster_defaults_unset` snapshots and restores
the seeded default instead of truncating it.

Before database cleanup, the telemetry barrier drains both its queued and held
batch. TRUNCATE keeps sequences advancing: a late writer then targets a dead ID,
not the next test's reused ID. Conflicting relation locks can require a bounded
deadlock retry. The test database owner disables append-only triggers only for
that cleanup transaction. Redis consumers flush only their admitted instance.

Session end detects new or modified OS jobs but never removes them: a concurrent
operator action might own the job. It also fails on excessive in-process memory;
macOS uses physical-footprint peak because compression hides anonymous memory
from RSS, falling back to ru_maxrss when its output lacks the gauge. The fixed
footprint command uses the owned bounded-process helper. Linux uses peak RSS.
Child Postgres/Redis memory is outside this gauge. These failure diagnostics go
to stderr. Finally the temporary home is removed; native fixture owners already
tear down their own processes and data directories.

Sync and async connection fixtures share these guards. Async pools use
check_connection and autocommit, and inbound listeners own a dedicated
reconnecting subscription that closes on teardown. Tests needing a particular
agent channel construct that listener explicitly.

```meiva-contract
id = "native-test-isolation"
kind = "invariant"
statement = "Native fixture cleanup covers every public per-test table, uses a private home and test-only URLs, and keeps the full-run selection guard active."
code = ["tests/fixtures/provisioning.py:<module>"]
tests = ["tests/contracts/test_lint_truncate_isolation.py::test_truncate_list_covers_every_public_table", "tests/harness/test_home_isolation.py::test_home_is_not_the_operators_real_home", "tests/harness/test_home_isolation.py::test_data_plane_and_gateway_urls_are_not_the_real_ones", "tests/fixtures/tests/test_full_run_guard.py::test_full_run_guard_decision"]
```
