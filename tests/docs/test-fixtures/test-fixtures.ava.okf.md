---
type: doc
title: "Test Fixtures — Root Plugins"
description: "The suite's global fixtures, autouse host guards and session hooks: the plugin modules in `tests/fixtures/` that the repo-root `conftest.py` loads, their load order, and the isolation invariants that order protects."
tags:
- evaluation
- quality-assurance
---

# Test Fixtures — Root Plugins

## What it is

Every test runs with a private `AVA_HOME`, lightweight autouse host guards and
default driver refusal. Native Postgres and Redis have independent standard
fixture dependencies; a pure test requests neither. Plugins under
`tests/fixtures/`, loaded by the repo-root `conftest.py` (`pytest_plugins` and a
fixture re-export: [[root-conftest.ava.okf.md]]), apply to every package's tests. Suite overview: [[tests.ava.okf.md]].

## Core mechanisms

### Plugin roster and load order
- `env_bootstrap` — import-time environment isolation and the session-wide runtime pins; loads **first**
- `leak_guard` — names the test that leaves process-global state different (environment, module attributes, cwd, signal handlers); loads **second**, so its fixture sets up first and compares last, after every function-scoped fixture's undo. Warn by default; findings reach CI through the shard JUnit reports: [[test-leak-guard.ava.okf.md]]
- `identity_restore` — restores the SDK consumer's explicit `ava.context` process slot through `sdk_identity`, so the hundreds of tests that call `pin_agent(...)` (`tests/fixtures/pin_agent.py`) need no undo; loads **third**, right after the guard: [[test-leak-guard.ava.okf.md]]
- `plugin_registrations` — per-test reset of plugin registrations
- `static_environment` — hook-only static process ownership; loads before its consumer `provisioning`
- `provisioning` — requested throwaway pg/redis, independent `_clean_db` / `_clean_redis`, DB connection fixtures, and session hooks (full-run guard, non-test-database refusal, leaked OS-job / runaway-memory / home cleanup)
- `guards` — autouse host guards and the `stub_everywhere` helper
- `guards` resource fixtures — default sync/async driver refusal, including held connections and Redis pipelines; `postgres_access` / `redis_access` admit only the declared owner's transport
- `path_scopes` — registers the per-directory fixture modules of `tests/path_scoped/` for the paths the `path_scopes.toml` files name (`PATH_SCOPES`, below); a plugin with hooks only, it adds no fixture of its own
- `_asyncio_stall_probe`, `collection_guard` — hook-only plugins (stall forensics; one collector node per directory)
- `pytester` — opt-in subprocess harness; no autouse fixtures
- **List order is load order and is load-bearing.** `leak_guard` precedes function isolation so it compares after restoration. Resource cleanup and access use ordinary fixture dependencies, rather than alphabetical autouse ordering.

### Consumer-local opt-in capabilities

Capability ownership: [[tests/fixtures/unit/docs/unit-fixtures.ava.okf.md]].

### Path-scoped fixtures

Existing fixture visibility and migration constraints: [[tests/docs/test-fixtures/path-scopes.ava.okf.md]].

### Isolation invariants
- `process_config` snapshots a session `ConfigBoot` only when a consumer requests configuration, before its function home. Provisioning publishes requested URLs to that owned value. `configuration.snapshot_process_config()` preserves explicit/default origins. SDK consumers bind `unit.sdk` locally for context and recorder isolation. SDK operation tests opt into `sdk_model_owner`; installer preconditions and settings stay separate.
- The pytest entry captures one immutable `LoadedCommit` in its stash. Opt-in `database_version` resolves that entry's native image before subprocess doubles; `database_gate` supplies independent nonexempt budgets shared by each test's builders. Provisioning supplies the explicit boot-owned machine reader to native migration setup. Assembled Gateway owners retain explicit machine inputs and overlapping lifespans borrow their original writer.
- **Per native xdist worker / session**, only requested instances start and persist for their owner's lifetime. Each DB consumer drains telemetry before TRUNCATE and again before revoking access; each Redis consumer starts with FLUSHDB. URLs in Settings, environment and the private `.env` restore after each function, including failed setup. Checkpoint schema is prepared once per requested database.
- Real consumers request `db_url` / `db_conn` / `database`, `redis_url` / `event_bus`, or an owner that depends on them. Assembled Gateway fixtures use `gateway_data_plane`, which also binds label and schedule guards. Tests owning their own native instance request only the corresponding access fixture and retain their existing resource cleanup. CLI and permissions-helper owners bind readiness, isolated port and private native-command guards beside their consumers; no resource inference or new path registry is involved.
- Native pool and client fixtures depend on their resource owner before opening workers; sibling test parameters do not define pool startup order. The shared Postgres session keeps its cluster, while each admitted function owns the existing stall watchdog through DB use and cleanup. Teardown requests stop and performs a bounded join; an in-flight probe or evidence capture that cannot join is an explicit failure, not proof of cancellation. Independently owned throwaway clusters retain their watchdog by default. Telemetry drains its actual emitter, then closes event and metrics pools before the DB access window ends.
- A killed run (Ctrl-C, SIGKILL, an agent dying mid-run) leaks its throwaway **Postgres**, because the detached postmaster outlives an owner that ran no finalizer. It is bounded not by teardown but by a **sweep at the start of the next spin-up**: `base.cluster.dataplane.pg_tools.sweep_orphaned_throwaway_clusters` reaps the instances whose owner is provably gone, proof being an exclusive `flock` the owner held for the instance's whole life on an `owner.lock` inside that instance's own dir (so the proof shares the cluster's exact lifetime, and two UNIX users on one `/dev/shm` never contend for a shared registry). The throwaway **redis** leaks the same way and is not swept (a redis orphan costs RAM, not the System V segment that wedges the box)
- **`env_bootstrap.py` stays first in root `pytest_plugins`, with environment isolation before every project import.** Configuration boot force-delivers its home's `.env`; importing first would capture the operator's production home and replace test sentinels. `_assert_env_precedes_project_imports()` rejects early imports; `tests/harness/test_home_isolation.py` verifies the result independently.
- A family of autouse **host-resource guards** makes "don't touch the host" the
  default: agent launch, permissions-helper native effects, OS cron, warm-up and
  `os.exec*`. Tests of those boundaries explicitly supply their own doubles or
  opt into an isolated native proof with owned cleanup. The `os.exec*` guard
  protects the test runner itself from process replacement.
- Plugin registrations (sections, namespaces, state fields) reset together after any test that loaded them, and the `_qualname` stamp `install_namespace` leaves on each namespace module taken off: autouse guard in `tests/fixtures/plugin_registrations.py`, wired via `pytest_plugins`

Static execution ownership and CI artifacts: [[tests/fixtures/docs/static-environment.ava.okf.md]].

Large test modules keep shared fixtures and test doubles in their existing owner.
Additional cases live in responsibility subdirectories, import those helpers
explicitly, and carry the same `path_scopes.toml` fixture dependencies. Module
marks and repository-root lookups follow the moved cases; no size baseline is
needed to collect or execute them.
