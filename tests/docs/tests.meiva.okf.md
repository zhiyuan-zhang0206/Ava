---
type: doc
title: "Test Suite"
description: "`tests/` is Meiva's traditional pytest test suite, covering all modules agent / gateway / cli / base."
tags:
- evaluation
- tool
- quality-assurance
---

# Test Suite

## What it is

`tests/` is Meiva's traditional pytest test suite, covering all modules agent / gateway / cli / base.

## Core responsibilities

### Test layers

| Layer | Location | Description |
|---|---|---|
| **Unit tests** | `<pkg>/**/tests/test_{file}.py` | one test file per source file, in the `tests/` directory of the package it tests (`tests/components/{module}/` holds only registered contract and integration tests; `scripts/structure/tests_location.py` refuses any other) |
| **Integration tests** | `tests/integration/` | cross-module tests; `TestClient` mounts `gateway.app:create_app` in-process + custom `_TestClientTransport` forwarding httpx, **no separate Gateway process** |
| **E2E tests** | `tests/e2e/` | full-stack end-to-end tests |
| **Data factories** | `tests/factories/` | test data construction tools |

`scripts/post_deploy_visual/layout_assertions.py` is the shared real-browser structural layer for
document overflow, viewport containment, center-point occlusion, nonempty blocks,
settle-before-capture, and the bounded wait that absorbs asynchronously mounted
panels before declared minimum visible counts are probed. Both the layout-invariant
suite and the post-deploy visual gate consume it so their definitions cannot drift.
`tests/e2e/visual/stubbed_home.py` holds the stubbed Home page the shell-geometry
suites drive: a fake EventSource, `/api/**` JSON stubs, and the shared context/open/settle helpers.

A package's own tests sit beside the code they prove (`base/packages/plugins/tests/`); integration tests across packages go in the lowest package that may legally import everything they use; end-to-end tests and contract tests that read repository artifacts stay in the top-level `tests/`. Bare `pytest` collects both trees through `testpaths` and `python_files`; `tests/ci/test_collection_roots.py` keeps them complete, so a tracked module under any `tests/` directory that pytest would silently skip (or a helper it would collect as a test) fails a test. The repo-root `conftest.py` plugins apply to every location alike. pyright holds a package's `tests/` directory to the tests type-checking standard through one generated `executionEnvironments` entry per directory (`scripts/codegen/gen_pyright_test_environments.py`; the `lint-pyright-test-environments` hook, unconditional at pre-push, and `tests/ci/test_pyright_test_environments.py` fail until it is re-run after a tests directory is added, moved or removed). CI prints how many tests each shard ran per `tests/` directory, and the total against the previous main run (`scripts/ci/shard_counts.py`), so a moved test that fell out of the suite shows as a changed count.

### Test layout

Component contracts, infrastructure checks and test discovery: [[test-layout.meiva.okf.md]].

### Global fixtures (repo-root `conftest.py` → `tests/fixtures/` plugins)
- The repo-root `conftest.py` loads global fixture plugins and explicitly re-exports shared non-autouse fixtures. Their load order and isolation contracts live in [[test-fixtures.meiva.okf.md]] and [[tests/docs/test-fixtures/root-conftest.meiva.okf.md]].
- Component conftests opt into their own fixture capabilities; remaining `path_scopes.toml` declarations mount the legacy fixture modules for their named consumers. See [[tests/fixtures/unit/docs/unit-fixtures.meiva.okf.md]] and [[tests/docs/test-fixtures/path-scopes.meiva.okf.md]].
- Some conftests also establish collection or process boundaries. In particular, retain `meiva/conftest.py` even without fixtures: it preserves the SDK package's import identity ([[meiva/docs/test-imports.meiva.okf.md]]). The e2e and native-root conftests retain their own process and configuration roles.

### CI integration

Validation ownership across hooks and CI: [[test-ci.meiva.okf.md]].

## Key dependencies

- [[agent/db/docs/db.meiva.okf.md]] — tests use isolated Postgres/Redis
- [[loop.meiva.okf.md]] — system under test
- [[gateway-cli.meiva.okf.md]] — integration tests mount `gateway.app:create_app` in-process (TestClient), no separate Gateway process needed

## Entry points

- `.venv/bin/pytest tests/components/agent/test_<subject>.py -q` — run the relevant subject
- `.venv/bin/pytest tests/components/lifecycle/db_authority/test_<subject>.py -q` — run a
  database-authority contract; native proof commands belong to their CI workflow
- Full-suite and coverage execution belong to CI, not local development

## Notes

- **Test strategy**: commit fast + CI full. Locally only run relevant tests, CI is the merge gate
- **Isolation**: each test gets isolated DB/Redis, avoiding parallel conflicts
- **Do not** run the full suite locally — Mac mini resources are limited (the `provisioning` and `env_bootstrap` plugins are designed for concurrency isolation: per-session database names + random free ports + Redis channel suffixes, **concurrent sessions will not conflict**, just resource-intensive)
- `gateway/http/middleware/tests/test_agent_error_wire_equivalence.py` parametrized verification of agent ↔ gateway error wire protocol consistency

### Browser frame sample scope

`tests/e2e/state/test_timeline_virtualization.py` takes the best p95 from three
serial sweeps to reduce load jitter. Its 33 ms gate compares uniform
`agent_chat` primary-row content. Expanded reasoning rows still check bounded
DOM and prepend behavior; this sample does not establish a frame-time bound
for every content shape or every run.

### Android update prompt harness

`tests/ui/test_android_update_check.py` runs the checked-in injected script
(`ui/app/src-tauri/scripts/update-check.js`) inside a Node VM with canned
command results, so every assertion observes the real source rather than a copy
of its selection rules.
