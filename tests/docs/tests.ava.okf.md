---
type: doc
title: "Test Suite"
description: "`tests/` is Ava's traditional pytest test suite, covering all modules agent / gateway / cli / base."
tags:
- evaluation
- tool
- quality-assurance
---

# Test Suite

## What it is

`tests/` is Ava's traditional pytest test suite, covering all modules agent / gateway / cli / base.

## Core responsibilities

### Test layers

| Layer | Location | Description |
|---|---|---|
| **Unit tests** | `<pkg>/**/tests/test_{file}.py` | one test file per source file, in the `tests/` directory of the package it tests (the top-level `tests/{module}/` holds only registered contract and integration tests; `scripts/structure/tests_location.py` refuses any other) |
| **Integration tests** | `tests/integration/` | cross-module tests; `TestClient` mounts `gateway.app` in-process + custom `_TestClientTransport` forwarding httpx, **no separate Gateway process** |
| **E2E tests** | `tests/e2e/` | full-stack end-to-end tests |
| **Data factories** | `tests/factories/` | test data construction tools |

`tests/e2e/visual/_layout_assertions.py` is the shared real-browser structural layer for
document overflow, viewport containment, center-point occlusion, nonempty blocks,
settle-before-capture, and the bounded wait that absorbs asynchronously mounted
panels before declared minimum visible counts are probed. It carries the
stubbed Home page the shell-geometry suites drive — a fake EventSource, `/api/**`
JSON stubs, and the shared context/open/settle helpers. Both the layout-invariant
suite and the post-deploy visual gate consume it so their definitions cannot drift.

A package's own tests sit beside the code they prove (`base/packages/plugins/tests/`); integration tests across packages go in the lowest package that may legally import everything they use; end-to-end tests and contract tests that read repository artifacts stay in the top-level `tests/`. Bare `pytest` collects both trees through `testpaths` and `python_files`; `tests/ci/test_collection_roots.py` keeps them complete, so a tracked module under any `tests/` directory that pytest would silently skip (or a helper it would collect as a test) fails a test. The repo-root `conftest.py` plugins apply to every location alike. pyright holds a package's `tests/` directory to the tests type-checking standard through one generated `executionEnvironments` entry per directory (`scripts/codegen/gen_pyright_test_environments.py`; the `lint-pyright-test-environments` hook, unconditional at pre-push, and `tests/ci/test_pyright_test_environments.py` fail until it is re-run after a tests directory is added, moved or removed). CI prints how many tests each shard ran per `tests/` directory, and the total against the previous main run (`scripts/ci/shard_counts.py`), so a moved test that fell out of the suite shows as a changed count.

### Test coverage scope
- `tests/agent/` — agent core (loop, graph, messages, state, hooks)
- `tests/ava/` — SDK surface (ava.* namespace)
- `tests/gateway/` — API gateway
- `tests/cli/` — command-line tools
- `tests/base/` — base library (`base/packages/*/tests/` holds the tests of `base/packages`)
- `tests/services/` — backend services
- `tests/ops/` — agent drain, pause/recovery, deployment holds, health inventory,
  service roster and resource admission
- `tests/lifecycle/` — lifecycle qualification, grouped by database authority,
  native root custody and the fleet update script
  (`preview/` holds the CI preview visual gate's golden-minting contracts).
  Native platform cases keep their explicit gates; moving the tests does not
  turn a simulated result into native evidence.
- `tests/skills/` — skills
- `tests/scripts/` — release/docs/secret-rotation and repository tooling;
  lint script tests also live at `tests/` root `test_lint_*.py`
- `tests/ui/` — deterministic tests for applying the shell's Kotlin/XML/signing overlay to Tauri's generated Android project
- `tests/plugins/` — cross-plugin tests (`test_grafana_dashboard_render.py`, `test_plugin_metrics_logql.py`); a plugin's own tests live in its package
- `tests/fixtures/` — the suite's global fixture plugins (see "Global fixtures" below) plus event fixture data; `tests/factories/` — data factories

- `scripts/tests/test_test_selector.py` — synthetic-checkout contracts for
  the static PR test selector, including queue, blind-file, duration, and
  process-determinism escapes
- `tests/scripts/test_ci_test_selection.py` — workflow contracts for the
  test-selection routing: the single mode switch, the enforced-subset gate,
  the shadow fallback, and the matching non-flaky pytest comparison

### Global fixtures (repo-root `conftest.py` → `tests/fixtures/` plugins)
- The repo-root `conftest.py` holds only `pytest_plugins`; the suite's global fixtures, host guards and session hooks are plugin modules in `tests/fixtures/`, so they apply to every test file in the repository rather than only the `tests/` tree. Plugin roster, load order and the isolation invariants: [[test-fixtures.ava.okf.md]]
- Directory-level fixtures (the former `conftest.py` files of `agent` / `ava` / `cli` / `gateway` / `integration` / `services` / `scripts/structure` / `lifecycle/db_authority`) are modules in `tests/path_scoped/`, registered for the paths named by the `path_scopes.toml` files next to the tests (`PATH_SCOPES`, read by `tests/fixtures/path_scopes.py`) so they follow a test into a package's `tests/` directory. A `conftest.py` remains only in `e2e` (package-scoped real processes), `lifecycle/native_root` (it must run without the repo-root conftest) and `services` (the win32 `collect_ignore`); details: [[test-fixtures.ava.okf.md]]

### CI integration

Validation ownership across hooks and CI: [[test-ci.ava.okf.md]].

## Key dependencies

- [[agent/db/docs/db.ava.okf.md]] — tests use isolated Postgres/Redis
- [[loop.ava.okf.md]] — system under test
- [[gateway-cli.ava.okf.md]] — integration tests mount `gateway.app` in-process (TestClient), no separate Gateway process needed

## Entry points

- `.venv/bin/pytest tests/agent/test_<subject>.py -q` — run the relevant subject
- `.venv/bin/pytest tests/lifecycle/db_authority/test_<subject>.py -q` — run a
  database-authority contract; native proof commands belong to their CI workflow
- Full-suite and coverage execution belong to CI, not local development

## Notes

- **Test strategy**: commit fast + CI full. Locally only run relevant tests, CI is the merge gate
- **Isolation**: each test gets isolated DB/Redis, avoiding parallel conflicts
- **Do not** run the full suite locally — Mac mini resources are limited (the `provisioning` and `env_bootstrap` plugins are designed for concurrency isolation: per-session database names + random free ports + Redis channel suffixes, **concurrent sessions will not conflict**, just resource-intensive)
- `gateway/middleware/tests/test_agent_error_wire_equivalence.py` parametrized verification of agent ↔ gateway error wire protocol consistency
