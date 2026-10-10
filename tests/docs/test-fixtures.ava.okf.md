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

Every test runs with a private `AVA_HOME` and autouse host guards. The default native environment also provisions throwaway Postgres + Redis. Plugins under `tests/fixtures/`, loaded by the repo-root `conftest.py` (which holds only `pytest_plugins`), apply to every package's tests. Suite overview: [[tests.ava.okf.md]].

## Core mechanisms

### Plugin roster and load order
- `env_bootstrap` — import-time environment isolation and the session-wide runtime pins; loads **first**
- `leak_guard` — names the test that leaves process-global state different (environment, module attributes, cwd, signal handlers); loads **second**, so its fixture sets up first and compares last, after every function-scoped fixture's undo. Warn by default; findings reach CI through the shard JUnit reports: [[test-leak-guard.ava.okf.md]]
- `identity_restore` — restores the SDK consumer's explicit `ava.context` process slot through `sdk_identity`, so the hundreds of tests that call `pin_agent(...)` (`tests/fixtures/pin_agent.py`) need no undo; loads **third**, right after the guard: [[test-leak-guard.ava.okf.md]]
- `plugin_registrations` — per-test reset of plugin registrations
- `static_environment` — hook-only static process ownership; loads before its consumer `provisioning`
- `provisioning` — throwaway pg/redis, `_clean_state`, the DB connection fixtures, and the session hooks (full-run guard, non-test-database refusal, leaked OS-job / runaway-memory / home cleanup)
- `guards` — autouse host guards and the `_stub_everywhere` helper
- `health_port_guard` — autouse protection against an unrelated process taking an isolated test port
- `path_scopes` — registers the per-directory fixture modules of `tests/path_scoped/` for the paths the `path_scopes.toml` files name (`PATH_SCOPES`, below); a plugin with hooks only, it adds no fixture of its own
- `_asyncio_stall_probe`, `collection_guard` — hook-only plugins (stall forensics; one collector node per directory)
- `pytester` — opt-in subprocess harness; no autouse fixtures
- **List order is load order and is load-bearing.** Same-scope autouse fixtures are set up in registration order and, inside one module, alphabetically — which is why `leak_guard` precedes everything (it must tear down last) and `provisioning` (`_clean_state`) precedes `guards` (`_guard_*`). Adding a plugin means checking its autouse names against that order.

### Consumer-local opt-in capabilities

Capability ownership: [[tests/fixtures/unit/docs/unit-fixtures.ava.okf.md]].

### Path-scoped fixtures (the former directory conftests)
- A conftest's fixtures reach only the tests below it, so a test moved into a package's `tests/` directory would silently lose them. The modules in `tests/path_scoped/` (one per former conftest, plus `api_keys` and `pty_reaper` that several take) are registered for the directories or single test files named by the `path_scopes.toml` files next to the tests (`PATH_SCOPES`, read by `tests/fixtures/path_scopes.py`) when pytest starts collecting that node (`parsefactories(holder=, node=)`, the interface pytest uses for a conftest). A governed test therefore sees exactly the fixture closure it saw under the conftest: the same autouse names in the same order, the same visibility, and a session-scoped autouse fixture (the provider-plugin load for the gateway tests) instantiated only when a governed test runs.
- The table is a migration device: the end state is each package's tests declaring the environment they need, or a local conftest providing it, so entries only come out. Moving a test edits the `path_scopes.toml` of the directory it leaves and of the one it enters (a moved directory carries its own file); `tests/ci/test_path_scopes.py` fails when a listed directory no longer holds a test file, and a listed path that no longer exists stops the run at configure. No count of test files is recorded (two moves that each adjusted it collided on every merge), so a test moved out of a listed directory without its new directory's `path_scopes.toml` naming it loses these fixtures silently: name it in the same change. The test also locks the pytest interface the plugin relies on; a pytest upgrade must re-check it.
- Hooks (`collect_ignore`, `pytest_configure`) are not fixtures and stay in their conftest: `services` (win32 `collect_ignore`), `lifecycle/native_root` (its `pytest_configure` must work without the repo-root conftest; the directory holds only that conftest today, and `tests/harness/test_home_isolation.py` asserts the hook defers to the root one), `e2e`.

### Isolation invariants
- Pytest stash owns the prepared process `ConfigBoot` before collection and bare homes; provisioning publishes native DB/Redis URLs to it. `configuration.snapshot_process_config()` gives session, Gateway and exec test roots one-time values with explicit/default origins intact, restoring bootstrap environment delivery. SDK consumers bind `unit.sdk` locally for context and recorder isolation. SDK operation tests opt into `sdk_model_owner`; installer preconditions and settings stay separate.
- **Per native xdist worker / session** a pair of throwaway pg/redis + per-session databases; per-test isolation via autouse TRUNCATE + checkpoint re-setup (**not** a full instance per test)
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
