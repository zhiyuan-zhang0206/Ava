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

Every test in the repository runs under the same isolation: a private `AVA_HOME`, a throwaway Postgres + Redis, and a set of autouse guards that keep a test off the host. It lives in plugin modules under `tests/fixtures/`, loaded by the repo-root `conftest.py` (which holds only `pytest_plugins`), so it applies to a test file in any package directory and not only under `tests/`. Overview of the suite: [[tests.ava.okf.md]].

## Core mechanisms

### Plugin roster and load order
- `env_bootstrap` — import-time environment isolation and the session-wide runtime pins; loads **first**
- `leak_guard` — names the test that leaves process-global state different (environment, module attributes, cwd, signal handlers); loads **second**, so its fixture sets up first and compares last, after every function-scoped fixture's undo. Warn by default; findings reach CI through the shard JUnit reports: [[test-leak-guard.ava.okf.md]]
- `identity_restore` — puts the agent-identity slots (`ava.agent_identity._agent_id` and its siblings, the turn contextvar) back after every test, so the hundreds of tests that assign `_agent_id` bare need no undo; loads **third**, right after the guard: [[test-leak-guard.ava.okf.md]]
- `plugin_registrations` — per-test reset of plugin registrations
- `provisioning` — throwaway pg/redis, `_clean_state`, the DB connection fixtures, and the session hooks (full-run guard, non-test-database refusal, leaked OS-job / runaway-memory / home cleanup)
- `guards` — autouse host guards and the `_stub_everywhere` helper
- `units` — gateway / runner unit, per-test unit home and workspace, write-generation ledger, `spawn_agent`
- `milvus`, `log_capture`, `retry_waits` — opt-in `milvus_client`, `loguru_records`, and `retry_waits` (records the waits `base.host.net.resilience` retry loops request instead of sleeping them; never autouse, because a no-op wait under a wall-clock-bounded loop spins until memory runs away, issue #1001)
- `path_scopes` — registers the per-directory fixture modules of `tests/path_scoped/` for the paths in its `PATH_SCOPES` table (below); a plugin with hooks only, it adds no fixture of its own
- `_asyncio_stall_probe`, `collection_guard` — hook-only plugins (stall forensics; one collector node per directory)
- **List order is load order and is load-bearing.** Same-scope autouse fixtures are set up in registration order and, inside one module, alphabetically — which is why `leak_guard` precedes everything (it must tear down last) and `provisioning` (`_clean_state`) precedes `guards` (`_guard_*`). Adding a plugin means checking its autouse names against that order.

### Path-scoped fixtures (the former directory conftests)
- A conftest's fixtures reach only the tests below it, so a test moved into a package's `tests/` directory would silently lose them. The modules in `tests/path_scoped/` (one per former conftest, plus `api_keys` and `pty_reaper` that several take) are registered for directories or single test files listed in `PATH_SCOPES` (`tests/fixtures/path_scopes.py`) when pytest starts collecting that node (`parsefactories(holder=, node=)`, the interface pytest uses for a conftest). A governed test therefore sees exactly the fixture closure it saw under the conftest: the same autouse names in the same order, the same visibility, and a session-scoped autouse fixture (the provider-plugin load for the gateway tests) instantiated only when a governed test runs.
- The table is a migration device: the end state is each package's tests declaring the environment they need, or a local conftest providing it, so entries only come out. Moving a test edits its `paths`; `tests/ci/test_path_scopes.py` fails when a listed directory no longer holds a test file, and a listed path that no longer exists stops the run at configure. No count of test files is recorded (two moves that each adjusted it collided on every merge), so a test moved out of a listed directory without its new path listed loses these fixtures silently: list the new path in the same change. The test also locks the pytest interface the plugin relies on; a pytest upgrade must re-check it.
- Hooks (`collect_ignore`, `pytest_configure`) are not fixtures and stay in their conftest: `services` (win32 `collect_ignore`), `lifecycle/native_root` (its `pytest_configure` must work without the repo-root conftest; the directory holds only that conftest today, and `tests/test_home_isolation.py` asserts the hook defers to the root one), `e2e`.

### Isolation invariants
- **Per xdist worker / session** a pair of throwaway pg/redis + per-session databases; per-test isolation via autouse TRUNCATE + checkpoint re-setup (**not** a full instance per test)
- A killed run (Ctrl-C, SIGKILL, an agent dying mid-run) leaks its throwaway **Postgres**, because the detached postmaster outlives an owner that ran no finalizer. It is bounded not by teardown but by a **sweep at the start of the next spin-up**: `base.cluster.dataplane.pg_tools.sweep_orphaned_throwaway_clusters` reaps the instances whose owner is provably gone, proof being an exclusive `flock` the owner held for the instance's whole life on an `owner.lock` inside that instance's own dir (so the proof shares the cluster's exact lifetime, and two UNIX users on one `/dev/shm` never contend for a shared registry). The throwaway **redis** leaks the same way and is not swept (a redis orphan costs RAM, not the System V segment that wedges the box)
- **The env block at the top of `tests/fixtures/env_bootstrap.py` must stay above every project import, and that module must stay first in the root `conftest.py`'s `pytest_plugins`.** `base.host.env.dotenv_boot.resolve_ava_home` reads `AVA_HOME` (else `~/.ava`) every time it is called, but the config boot loads `$AVA_HOME/.env` when `base.config` is first imported, so AVA_HOME set after that import leaves the suite booted from the operator's real `~/.ava/.env`, and `_enforce_cluster_env_authority()` force-assigns the production cluster secret / db / redis / gateway URL over the sentinels the suite just set. `_assert_env_precedes_project_imports()` fails the run if a project module was imported early; `tests/test_home_isolation.py` asserts the outcome independently of mechanism
- A family of autouse **host-resource guards** makes "don't touch the host" the
  default: agent launch, permissions-helper native effects, OS cron, warm-up and
  `os.exec*`. Tests of those boundaries explicitly supply their own doubles or
  opt into an isolated native proof with owned cleanup. The `os.exec*` guard
  protects the test runner itself from process replacement.
- Plugin registrations (sections, namespaces, state fields) reset together after any test that loaded them, and the `_qualname` stamp `register_namespace` leaves on each namespace module taken off: autouse guard in `tests/fixtures/plugin_registrations.py`, wired via `pytest_plugins`
